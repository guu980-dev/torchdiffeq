import os
import argparse
import logging
import time
import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader
import torchvision.datasets as datasets
import torchvision.transforms as transforms
import matplotlib.pyplot as plt
import torch.optim as optim
from torch.optim.lr_scheduler import StepLR

parser = argparse.ArgumentParser()
parser.add_argument('--network', type=str, choices=['resnet', 'odenet'], default='odenet')
parser.add_argument('--tol', type=float, default=1e-3)
parser.add_argument('--adjoint', type=eval, default=False, choices=[True, False])
parser.add_argument('--downsampling-method', type=str, default='conv', choices=['conv', 'res'])
parser.add_argument('--nepochs', type=int, default=160)
parser.add_argument('--data_aug', type=eval, default=True, choices=[True, False])
parser.add_argument('--lr', type=float, default=0.1)
parser.add_argument('--batch_size', type=int, default=128)
parser.add_argument('--test_batch_size', type=int, default=1000)
parser.add_argument('--solver_method', type=str, default='default', choices=['default', 'euler', 'dopri5', 'rk4', ]) # default = dopri5

parser.add_argument('--save', type=str, default='./experiment1')
parser.add_argument('--debug', action='store_true')
parser.add_argument('--gpu', type=int, default=0)
args = parser.parse_args()

if args.adjoint:
    from torchdiffeq import odeint_adjoint as odeint
else:
    from torchdiffeq import odeint


def conv3x3(in_planes, out_planes, stride=1):
    """3x3 convolution with padding"""
    return nn.Conv2d(in_planes, out_planes, kernel_size=3, stride=stride, padding=1, bias=False)


def conv1x1(in_planes, out_planes, stride=1):
    """1x1 convolution"""
    return nn.Conv2d(in_planes, out_planes, kernel_size=1, stride=stride, bias=False)


def norm(dim):
    return nn.GroupNorm(min(32, dim), dim)


class ResBlock(nn.Module):
    expansion = 1

    def __init__(self, inplanes, planes, stride=1, downsample=None):
        super(ResBlock, self).__init__()
        self.norm1 = norm(inplanes)
        self.relu = nn.ReLU(inplace=True)
        self.downsample = downsample
        self.conv1 = conv3x3(inplanes, planes, stride)
        self.norm2 = norm(planes)
        self.conv2 = conv3x3(planes, planes)

    def forward(self, x):
        shortcut = x

        out = self.relu(self.norm1(x))

        if self.downsample is not None:
            shortcut = self.downsample(out)

        out = self.conv1(out)
        out = self.norm2(out)
        out = self.relu(out)
        out = self.conv2(out)

        return out + shortcut


class ConcatConv2d(nn.Module):

    def __init__(self, dim_in, dim_out, ksize=3, stride=1, padding=0, dilation=1, groups=1, bias=True, transpose=False):
        super(ConcatConv2d, self).__init__()
        module = nn.ConvTranspose2d if transpose else nn.Conv2d
        self._layer = module(
            dim_in + 1, dim_out, kernel_size=ksize, stride=stride, padding=padding, dilation=dilation, groups=groups,
            bias=bias
        )

    def forward(self, t, x):
        tt = torch.ones_like(x[:, :1, :, :]) * t
        ttx = torch.cat([tt, x], 1)
        return self._layer(ttx)


class ODEfunc(nn.Module):

    def __init__(self, dim):
        super(ODEfunc, self).__init__()
        self.norm1 = norm(dim)
        self.relu = nn.ReLU(inplace=True)
        self.conv1 = ConcatConv2d(dim, dim, 3, 1, 1)
        self.norm2 = norm(dim)
        self.conv2 = ConcatConv2d(dim, dim, 3, 1, 1)
        self.norm3 = norm(dim)
        self.conv3 = ConcatConv2d(dim, dim, 3, 1, 1)
        self.norm4 = norm(dim)
        self.nfe = 0

    def forward(self, t, x):
        self.nfe += 1
        out = self.norm1(x)
        out = self.relu(out)
        out = self.conv1(t, out)
        out = self.norm2(out)
        out = self.relu(out)
        out = self.conv2(t, out)
        out = self.norm3(out)
        out = self.conv3(t, out)
        out = self.norm4(out)
        return out


class ODEBlock(nn.Module):

    def __init__(self, odefunc, method):
        super(ODEBlock, self).__init__()
        self.odefunc = odefunc
        self.method = method
        self.integration_time = torch.tensor([0, 1]).float()

    def forward(self, x):
        self.integration_time = self.integration_time.type_as(x)
        out = odeint(self.odefunc, x, self.integration_time, rtol=args.tol, atol=args.tol, method=self.method)
        return out[1]

    @property
    def nfe(self):
        return self.odefunc.nfe

    @nfe.setter
    def nfe(self, value):
        self.odefunc.nfe = value


class Flatten(nn.Module):

    def __init__(self):
        super(Flatten, self).__init__()

    def forward(self, x):
        shape = torch.prod(torch.tensor(x.shape[1:])).item()
        return x.view(-1, shape)


class RunningAverageMeter(object):
    """Computes and stores the average and current value"""

    def __init__(self, momentum=0.99):
        self.momentum = momentum
        self.reset()

    def reset(self):
        self.val = None
        self.avg = 0

    def update(self, val):
        if self.val is None:
            self.avg = val
        else:
            self.avg = self.avg * self.momentum + val * (1 - self.momentum)
        self.val = val

class CustomRunningAverageMeter(RunningAverageMeter):
    def __init__(self):
      super()
      self.reset()

    def reset(self):
      super().reset()
      self.values = []

    def update(self, val):
      self.values.append(val)
      self.val = val
    
    def total_val(self):
      return sum(self.values)

    def real_avg(self):
      return self.total_val() / len(self.values)

def get_mnist_loaders(data_aug=False, batch_size=128, test_batch_size=1000, perc=1.0):
    if data_aug:
        transform_train = transforms.Compose([
            transforms.RandomCrop(28, padding=4),
            transforms.ToTensor(),
        ])
    else:
        transform_train = transforms.Compose([
            transforms.ToTensor(),
        ])

    transform_test = transforms.Compose([
        transforms.ToTensor(),
    ])

    train_loader = DataLoader(
        datasets.MNIST(root='.data/mnist', train=True, download=True, transform=transform_train), batch_size=batch_size,
        shuffle=True, num_workers=2, drop_last=True
    )

    train_eval_loader = DataLoader(
        datasets.MNIST(root='.data/mnist', train=True, download=True, transform=transform_test),
        batch_size=test_batch_size, shuffle=False, num_workers=2, drop_last=True
    )

    test_loader = DataLoader(
        datasets.MNIST(root='.data/mnist', train=False, download=True, transform=transform_test),
        batch_size=test_batch_size, shuffle=False, num_workers=2, drop_last=True
    )

    return train_loader, test_loader, train_eval_loader


def get_cifar10_loaders(data_aug=False, batch_size=128, test_batch_size=1000, perc=1.0):
    if data_aug:
        transform_train = transforms.Compose([
            transforms.RandomCrop(32, padding=4),
            transforms.RandomHorizontalFlip(),
            transforms.ToTensor(),
            transforms.Normalize((0.4914, 0.4822, 0.4465), (0.2023, 0.1994, 0.2010)),
        ])
    else:
        transform_train = transforms.Compose([
            transforms.ToTensor(),
            transforms.Normalize((0.4914, 0.4822, 0.4465), (0.2023, 0.1994, 0.2010)),
        ])

    transform_test = transforms.Compose([
        transforms.ToTensor(),
        transforms.Normalize((0.4914, 0.4822, 0.4465), (0.2023, 0.1994, 0.2010)),
    ])

    train_loader = DataLoader(
        datasets.CIFAR10(root='.data/cifar10', train=True, download=True, transform=transform_train),
        batch_size=batch_size, shuffle=True, num_workers=2, drop_last=True
    )

    train_eval_loader = DataLoader(
        datasets.CIFAR10(root='.data/cifar10', train=True, download=True, transform=transform_test),
        batch_size=test_batch_size, shuffle=False, num_workers=2, drop_last=True
    )

    test_loader = DataLoader(
        datasets.CIFAR10(root='.data/cifar10', train=False, download=True, transform=transform_test),
        batch_size=test_batch_size, shuffle=False, num_workers=2, drop_last=True
    )

    return train_loader, test_loader, train_eval_loader


def inf_generator(iterable):
    """Allows training with DataLoaders in a single infinite loop:
        for i, (x, y) in enumerate(inf_generator(train_loader)):
    """
    iterator = iterable.__iter__()
    while True:
        try:
            yield iterator.__next__()
        except StopIteration:
            iterator = iterable.__iter__()


def learning_rate_with_decay(batch_size, batch_denom, batches_per_epoch, boundary_epochs, decay_rates):
    initial_learning_rate = args.lr * batch_size / batch_denom

    boundaries = [int(batches_per_epoch * epoch) for epoch in boundary_epochs]
    vals = [initial_learning_rate * decay for decay in decay_rates]

    def learning_rate_fn(itr):
        lt = [itr < b for b in boundaries] + [True]
        i = np.argmax(lt)
        return vals[i]

    return learning_rate_fn


def one_hot(x, K):
    return np.array(x[:, None] == np.arange(K)[None, :], dtype=int)


def accuracy(model, dataset_loader):
    total_correct = 0
    for x, y in dataset_loader:
        x = x.to(device)
        y = one_hot(np.array(y.numpy()), 10)

        target_class = np.argmax(y, axis=1)
        predicted_class = np.argmax(model(x).cpu().detach().numpy(), axis=1)
        total_correct += np.sum(predicted_class == target_class)
    return total_correct / len(dataset_loader.dataset)


def count_parameters(model):
    return sum(p.numel() for p in model.parameters() if p.requires_grad)


def makedirs(dirname):
    if not os.path.exists(dirname):
        os.makedirs(dirname)


def get_logger(logpath, filepath, package_files=[], displaying=True, saving=True, debug=False):
    logger = logging.getLogger()

    # skip if logger already exist
    if (len(logger.handlers) > 0):
        return logger

    if debug:
        level = logging.DEBUG
    else:
        level = logging.INFO
    logger.setLevel(level)
    if saving:
        info_file_handler = logging.FileHandler(logpath, mode="a")
        info_file_handler.setLevel(level)
        logger.addHandler(info_file_handler)
    if displaying:
        console_handler = logging.StreamHandler()
        console_handler.setLevel(level)
        logger.addHandler(console_handler)
    logger.info(filepath)
    with open(filepath, "r") as f:
        logger.info(f.read())

    for f in package_files:
        logger.info(f)
        with open(f, "r") as package_f:
            logger.info(package_f.read())

    return logger

def plot_accuracy(train_accs, test_accs, epochs):
    plt.figure(figsize=(10, 5))
    plt.plot(epochs, train_accs, label='Train Accuracy')
    plt.plot(epochs, test_accs, label='Test Accuracy')
    plt.xlabel('Epoch')
    plt.ylabel('Accuracy')
    plt.title('Train and Test Accuracy over Epochs')
    plt.legend()
    plt.grid(True)
    plt.show()

def plot_average_batch_time(average_times, epochs):
    plt.figure(figsize=(10, 5))
    plt.plot(epochs, average_times, label='Average Batch Time')
    plt.xlabel('Epoch')
    plt.ylabel('Time (seconds)')
    plt.title('Average Wall Clock Time per Batch over Epochs')
    plt.legend()
    plt.grid(True)
    plt.show()

def plot_total_batch_time(total_times, epochs):
    plt.figure(figsize=(10, 5))
    plt.plot(epochs, total_times, label='Total Batch Time')
    plt.xlabel('Epoch')
    plt.ylabel('Time (seconds)')
    plt.title('Total Wall Clock Time over Epochs')
    plt.legend()
    plt.grid(True)
    plt.show()


if __name__ == '__main__':

    makedirs(args.save)
    logger = get_logger(logpath=os.path.join(args.save, 'logs'), filepath=os.path.abspath(__file__))
    logger.info(args)

    device = torch.device('cuda:' + str(args.gpu) if torch.cuda.is_available() else 'cpu')

    is_odenet = args.network == 'odenet'

    if args.downsampling_method == 'conv':
        downsampling_layers = [
            nn.Conv2d(3, 128, 3, 1),
            norm(128),
            nn.ReLU(inplace=True),
            nn.Conv2d(128, 128, 4, 2, 1),
            norm(128),
            nn.ReLU(inplace=True),
            nn.Conv2d(128, 128, 4, 2, 1),
        ]
    elif args.downsampling_method == 'res':
        downsampling_layers = [
            nn.Conv2d(3, 128, 3, 1),
            ResBlock(128, 128, stride=2, downsample=conv1x1(128, 128, 2)),
            ResBlock(128, 128, stride=2, downsample=conv1x1(128, 128, 2)),
        ]

    ode_solver_method = None if args.solver_method == 'default' else args.solver_method
    feature_layers = [ODEBlock(ODEfunc(128), ode_solver_method)] if is_odenet else [ResBlock(128, 128) for _ in range(6)]
    fc_layers = [norm(128), nn.ReLU(inplace=True), nn.AdaptiveAvgPool2d((1, 1)), Flatten(), nn.Linear(128, 10), nn.Dropout(p=0.5)]

    model = nn.Sequential(*downsampling_layers, *feature_layers, *fc_layers).to(device)

    logger.info(model)
    logger.info('Number of parameters: {}'.format(count_parameters(model)))

    criterion = nn.CrossEntropyLoss().to(device)

    # train_loader, test_loader, train_eval_loader = get_mnist_loaders(
    #     args.data_aug, args.batch_size, args.test_batch_size
    # )

    train_loader, test_loader, train_eval_loader = get_cifar10_loaders(
        args.data_aug, args.batch_size, args.test_batch_size
    )

    data_gen = inf_generator(train_loader)
    batches_per_epoch = len(train_loader)

    lr_fn = learning_rate_with_decay(
        args.batch_size, batch_denom=128, batches_per_epoch=batches_per_epoch, boundary_epochs=[60, 100, 140],
        decay_rates=[1, 0.1, 0.01, 0.001]
    )

    optimizer = torch.optim.SGD(model.parameters(), lr=args.lr, momentum=0.9)

    best_acc = 0
    batch_time_meter = RunningAverageMeter()
    custom_batch_time_meter = CustomRunningAverageMeter()
    f_nfe_meter = RunningAverageMeter()
    b_nfe_meter = RunningAverageMeter()
    end = time.time()

    train_accuracies = []
    test_accuracies = []
    average_times = []
    total_times = []
    epochs = []
    
    # StepLR 스케줄러 초기화
    scheduler = StepLR(optimizer, step_size=30, gamma=0.1)

    for itr in range(args.nepochs * batches_per_epoch):

        for param_group in optimizer.param_groups:
            param_group['lr'] = lr_fn(itr)

        optimizer.zero_grad()
        x, y = data_gen.__next__()
        x = x.to(device)
        y = y.to(device)
        logits = model(x)
        loss = criterion(logits, y)

        if is_odenet:
            nfe_forward = feature_layers[0].nfe
            feature_layers[0].nfe = 0

        loss.backward()
        optimizer.step()

        if is_odenet:
            nfe_backward = feature_layers[0].nfe
            feature_layers[0].nfe = 0

        time_var = time.time() - end
        batch_time_meter.update(time_var)
        custom_batch_time_meter.update(time_var)
        if is_odenet:
            f_nfe_meter.update(nfe_forward)
            b_nfe_meter.update(nfe_backward)
        end = time.time()

        if itr % batches_per_epoch == 0:
            scheduler.step()
            with torch.no_grad():
                model.eval()  # 평가 모드로 전환하여 Dropout 비활성화
                train_acc = accuracy(model, train_eval_loader)
                val_acc = accuracy(model, test_loader)
                if val_acc > best_acc:
                    torch.save({'state_dict': model.state_dict(), 'args': args}, os.path.join(args.save, 'model.pth'))
                    best_acc = val_acc
                # logger.info(
                print(
                    "Epoch {:04d} | Time {:.3f} ({:.3f}) | NFE-F {:.1f} | NFE-B {:.1f} | "
                    "Train Acc {:.4f} | Test Acc {:.4f} | Average Wall Clock Time for batch in epoch {:.3f} | "
                    "Total Wall Clock Time in epoch {:.3f} | batch number per epoch {:04d}".format(
                        itr // batches_per_epoch, batch_time_meter.val, batch_time_meter.avg, f_nfe_meter.avg, b_nfe_meter.avg,
                        train_acc, val_acc, custom_batch_time_meter.real_avg(),
                        custom_batch_time_meter.total_val(), batches_per_epoch,
                    )
                )

                train_accuracies.append(train_acc)
                test_accuracies.append(val_acc)
                average_times.append(custom_batch_time_meter.real_avg())
                total_times.append(custom_batch_time_meter.total_val())
                epochs.append(itr // batches_per_epoch)

                custom_batch_time_meter.reset()
                model.train()  # 학습 모드로 다시 전환

    plot_accuracy(train_accuracies[1:], test_accuracies[1:], epochs[1:])
    plot_average_batch_time(average_times[1:], epochs[1:])
    plot_total_batch_time(total_times[1:], epochs[1:])
    print('total average wall clock time per epoch {:.2f}'.format(sum(total_times)/len(total_times)))