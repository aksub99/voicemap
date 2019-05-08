import torch
import torch.nn.functional as F
import numpy as np
from torch import optim, nn
from torch.utils.data import DataLoader
# from torchvision.datasets import DatasetFolder
from sklearn.model_selection import train_test_split
from multiprocessing import cpu_count
from functools import reduce
import argparse
from olympic.callbacks import CSVLogger, Evaluate, ReduceLROnPlateau, ModelCheckpoint
from olympic import fit

from voicemap.datasets import LibriSpeech, SpeakersInTheWild, ClassConcatDataset, SpectrogramDataset, DatasetFolder
from voicemap.models import ResidualClassifier, BaselineClassifier
from voicemap.utils import whiten, setup_dirs
from voicemap.eval import VerificationMetrics
from config import PATH, DATA_PATH


setup_dirs()
assert torch.cuda.is_available()
device = torch.device('cuda')
torch.backends.cudnn.benchmark = True


##############
# Parameters #
##############
parser = argparse.ArgumentParser()
parser.add_argument('--model', type=str)
parser.add_argument('--dim', type=int)
parser.add_argument('--lr', type=float, help='Initial learning rate.')
parser.add_argument('--weight-decay', type=float)
parser.add_argument('--momentum', type=float)
parser.add_argument('--epochs', type=int)
parser.add_argument('--filters', type=int)
parser.add_argument('--batch-size', type=int)
parser.add_argument('--n-seconds', type=float)
parser.add_argument('--downsampling', type=int)
parser.add_argument('--spectrogram', type=lambda x: x.lower()[0] == 't', default=True,
                    help='Whether or not to use raw waveform or a spectogram as inputs.')
parser.add_argument('--precompute-spect', type=lambda x: x.lower()[0] == 't', default=True,
                    help='Whether or not to calculate spectrograms on the fly from raw audio.')
parser.add_argument('--window-length', type=float, help='STFT window length in seconds.')
parser.add_argument('--window-hop', type=float, help='STFT window hop in seconds.')
args = parser.parse_args()

param_str = '__'.join([f'{k}={str(v)}' for k, v in vars(args).items()])

print(param_str)

val_fraction = 0.1

if args.spectrogram:
    if args.dim == 1:
        in_channels = int(args.window_length * 16000) // 2 + 1
    elif args.dim == 2:
        in_channels = 1
    else:
        raise RuntimeError
else:
    in_channels = 1


###################
# Create datasets #
###################
librispeech_subsets = ['train-clean-100', 'train-clean-360', 'train-other-500']
# librispeech_subsets = ['dev-clean']

if args.spectrogram:
    if args.precompute_spect:
        def random_crop(n):
            def _random_crop(spect):
                start_index = np.random.randint(0, max(len(spect)-n, 1))

                # Zero pad
                if spect.shape[-1] < n:
                    less_timesteps = n - spect.shape[-1]
                    spect = np.pad(spect, ((0, 0), (0, 0), (0, less_timesteps)), 'constant')

                if args.dim == 1:
                    spect = spect[0, :, start_index:start_index+n]
                else:
                    spect = spect[:, :, start_index:start_index + n]

                return spect

            return _random_crop

        transform = random_crop(int(args.n_seconds / args.window_hop))
        librispeech = ClassConcatDataset([
            DatasetFolder(
                DATA_PATH + f'/LibriSpeech.spec/{subset}/', extensions=['.npy'], loader=np.load, transform=transform)
            for subset in librispeech_subsets
        ])
        librispeech_unseen = DatasetFolder(DATA_PATH + f'/LibriSpeech.spec/dev-clean/', extensions=['.npy'],
                                           loader=np.load, transform=transform)
        sitw = DatasetFolder(DATA_PATH + '/sitw.spec/dev/', extensions=['.npy'], loader=np.load, transform=transform)
        # speaker_ids = reduce(lambda x, y: x + y, [d.classes for d in librispeech])  # + sitw.classes
    else:
        librispeech = SpectrogramDataset(
            LibriSpeech(librispeech_subsets, args.n_seconds, args.downsampling, stochastic=True, pad=False),
            normalisation='global',
            window_length=args.window_length,
            window_hop=args.window_hop
        )
        librispeech_unseen = SpectrogramDataset(
            LibriSpeech('dev-clean', args.n_seconds, args.downsampling, stochastic=True, pad=False),
            normalisation='global',
            window_length=args.window_length,
            window_hop=args.window_hop
        )
        sitw = SpectrogramDataset(
            SpeakersInTheWild('dev', 'enroll-core', args.n_seconds, args.downsampling, stochastic=True, pad=False),
            normalisation='global',
            window_length=args.window_length,
            window_hop=args.window_hop
        )
        # speaker_ids = librispeech.df['speaker_id'].values.tolist()  # + sitw.df['speaker_id'].values.tolist()
else:
    librispeech = LibriSpeech(librispeech_subsets, args.n_seconds, args.downsampling, stochastic=True, pad=False)
    sitw = SpeakersInTheWild('dev', 'enroll-core', args.n_seconds, args.downsampling, stochastic=True, pad=False)
    librispeech_unseen = LibriSpeech('dev-clean', args.n_seconds, args.downsampling, stochastic=True, pad=False)
    # speaker_ids = librispeech.df['speaker_id'].values.tolist()  # + sitw.df['speaker_id'].values.tolist()


# data = ClassConcatDataset([librispeech, sitw])
data = librispeech
num_classes = data.num_classes
print(f'Total no. speakers = {num_classes}')

indices = range(len(data))
train_indices, test_indices, _, _ = train_test_split(
    indices,
    indices,
    test_size=val_fraction,
    # stratify=speaker_ids
)

train = torch.utils.data.Subset(data, train_indices)
val = torch.utils.data.Subset(data, test_indices)


################
# Define model #
################
if args.model == 'resnet':
    model = ResidualClassifier(in_channels, args.filters, [2, 2, 2, 2], num_classes, dim=args.dim)
elif args.model == 'baseline':
    model = BaselineClassifier(in_channels, args.filters, 256, num_classes, dim=args.dim)
else:
    raise RuntimeError
model.to(device, dtype=torch.double)


############
# Training #
############
train_loader = DataLoader(train, batch_size=args.batch_size, num_workers=cpu_count(), shuffle=True, drop_last=True)
val_loader = DataLoader(val, batch_size=args.batch_size, num_workers=cpu_count(), shuffle=True, drop_last=True)
opt = optim.SGD(model.parameters(), lr=args.lr, momentum=args.momentum, weight_decay=args.weight_decay)
loss_fn = nn.CrossEntropyLoss()


if args.spectrogram:
    def prepare_batch(batch):
        # Normalise inputs
        # Move to GPU and convert targets to int
        x, y = batch
        return x.double().cuda(), y.long().cuda()
else:
    def prepare_batch(batch):
        # Normalise inputs
        # Move to GPU and convert targets to int
        x, y = batch
        return whiten(x).cuda(), y.long().cuda()


def gradient_step(model, optimiser, loss_fn, x, y, epoch):
    # Slight modification of regular gradient step to
    model.train()
    optimiser.zero_grad()
    y_pred = model(x, y)
    loss = loss_fn(y_pred, y)
    loss.backward()
    optimiser.step()

    return loss, y_pred


callbacks = [
    Evaluate(val_loader),
    VerificationMetrics(sitw, num_pairs=25000, prefix='sitw_dev_'),
    VerificationMetrics(librispeech_unseen, num_pairs=25000, prefix='librispeech_dev_clean_'),
    ReduceLROnPlateau(monitor='val_loss', patience=5, verbose=False, min_delta=0.1),
    ModelCheckpoint(filepath=PATH + f'/models/classifier_{param_str}.pt',
                    monitor='val_loss', save_best_only=True, verbose=False),
    CSVLogger(PATH + f'/logs/classifier_{param_str}.csv'),
]

fit(
    model,
    opt,
    loss_fn,
    epochs=args.epochs,
    dataloader=train_loader,
    prepare_batch=prepare_batch,
    callbacks=callbacks,
    metrics=['accuracy'],
    update_fn=gradient_step
)
