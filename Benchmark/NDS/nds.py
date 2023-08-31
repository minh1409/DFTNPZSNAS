from .config import cfg
import torch
import torch.nn as nn
from .operations import ReLUConvBN, FactorizedReduce, Identity, OPS
from .common import Preprocess, Classifier
from LearnableParams.constructedblock import convolutions, pseudoBatchNorm, LinearLayer
from .genotypes import GENOTYPES, Genotype

def drop_path(x, drop_prob):
    """Drop path (ported from DARTS)."""
    if drop_prob > 0.:
        keep_prob = 1.-drop_prob
        mask = torch.autograd.Variable(
            torch.cuda.FloatTensor(x.size(0), 1, 1, 1).bernoulli_(keep_prob)
        )
        x.div_(keep_prob)
        x.mul_(mask)
    return x


class Cell(nn.Module):
    """NAS cell (ported from DARTS)."""

    def __init__(self, representative_params, genotype, C_prev_prev, C_prev, C, reduction, reduction_prev):
        super(Cell, self).__init__()
        # logger.info('{}, {}, {}'.format(C_prev_prev, C_prev, C))
        self.representative_params = representative_params
        if reduction_prev:
            self.preprocess0 = FactorizedReduce(representative_params, C_prev_prev, C)
        else:
            self.preprocess0 = ReLUConvBN(representative_params, C_prev_prev, C, 1, 1, 0)
        self.preprocess1 = ReLUConvBN(representative_params, C_prev, C, 1, 1, 0)

        if reduction:
            op_names, indices = zip(*genotype.reduce)
            concat = genotype.reduce_concat
        else:
            op_names, indices = zip(*genotype.normal)
            concat = genotype.normal_concat
        self._compile(C, op_names, indices, concat, reduction)
        

    def _compile(self, C, op_names, indices, concat, reduction):
        assert len(op_names) == len(indices)
        self._steps = len(op_names) // 2
        self._concat = concat
        self.multiplier = len(concat)

        self._ops = nn.ModuleList()
        for name, index in zip(op_names, indices):
            stride = 2 if reduction and index < 2 else 1
            op = OPS[name](self.representative_params, C, stride, True)
            self._ops += [op]
        self._indices = indices

    def forward(self, s0, s1, drop_prob):
        s0 = self.preprocess0(s0)
        s1 = self.preprocess1(s1)

        states = [s0, s1]
        for i in range(self._steps):
            h1 = states[self._indices[2*i]]
            h2 = states[self._indices[2*i+1]]

            op1 = self._ops[2*i]
            op2 = self._ops[2*i+1]
            h1 = op1(h1)
            h2 = op2(h2)

            if self.training and drop_prob > 0.:
                if not isinstance(op1, Identity):
                    h1 = drop_path(h1, drop_prob)
                if not isinstance(op2, Identity):
                    h2 = drop_path(h2, drop_prob)
            s = h1 + h2
            states += [s]
        return torch.cat([states[i] for i in self._concat], dim=1)


class AuxiliaryHeadCIFAR(nn.Module):

    def __init__(self, representative_params, C, num_classes):
        """assuming input size 8x8"""
        super(AuxiliaryHeadCIFAR, self).__init__()
        self.features = nn.Sequential(
            nn.ReLU(inplace=False),
            nn.AvgPool2d(5, stride=3, padding=0, count_include_pad=False), # image size = 2 x 2
            # nn.Conv2d(C, 128, 1, bias=False),
            # nn.BatchNorm2d(128),
            convolutions(representative_params, in_channels=C, out_channels=128, kernel_size=1, stride=1, padding=0),
            pseudoBatchNorm(),
            nn.ReLU(inplace=False),
            # nn.Conv2d(128, 768, 2, bias=False),
            # nn.BatchNorm2d(768),
            convolutions(representative_params, in_channels=128, out_channels=768, kernel_size=2, stride=1, padding=0),
            pseudoBatchNorm(),
            nn.ReLU(inplace=False)
        )
        # self.classifier = nn.Linear(768, num_classes)
        self.classifier = LinearLayer(representative_params, 768, num_classes)

    def forward(self, x):
        x = self.features(x)
        x = self.classifier(x.view(x.size(0),-1))
        return x


class AuxiliaryHeadImageNet(nn.Module):

    def __init__(self, representative_params, C, num_classes):
        """assuming input size 14x14"""
        super(AuxiliaryHeadImageNet, self).__init__()
        self.features = nn.Sequential(
            nn.ReLU(inplace=False),
            nn.AvgPool2d(5, stride=2, padding=0, count_include_pad=False),

            convolutions(representative_params, in_channels=C, out_channels=128, kernel_size=1, stride=1, padding=0),
            pseudoBatchNorm(),
            nn.ReLU(inplace=False),
            convolutions(representative_params, in_channels=128, out_channels=768, kernel_size=2, stride=1, padding=0),
            pseudoBatchNorm(),
            nn.ReLU(inplace=False)
        )
        self.classifier = LinearLayer(representative_params, 768, num_classes)

    def forward(self, x):
        x = self.features(x)
        x = self.classifier(x.view(x.size(0),-1))
        return x


class NetworkCIFAR(nn.Module):
    """CIFAR network (ported from DARTS)."""

    def __init__(self, representative_params, C, num_classes, layers, auxiliary, genotype):
        super(NetworkCIFAR, self).__init__()
        self._layers = layers
        self._auxiliary = auxiliary

        stem_multiplier = 3
        C_curr = stem_multiplier*C
        self.stem = nn.Sequential(
            convolutions(representative_params, in_channels=cfg.MODEL.INPUT_CHANNELS, out_channels=C_curr, kernel_size=3, stride=1, padding=1),
            pseudoBatchNorm(),
        )

        C_prev_prev, C_prev, C_curr = C_curr, C_curr, C
        self.cells = nn.ModuleList()
        reduction_prev = False
        for i in range(layers):
            if i in [layers//3, 2*layers//3]:
                C_curr *= 2
                reduction = True
            else:
                reduction = False
            cell = Cell(representative_params, genotype, C_prev_prev, C_prev, C_curr, reduction, reduction_prev)
            reduction_prev = reduction
            self.cells += [cell]
            C_prev_prev, C_prev = C_prev, cell.multiplier*C_curr
            if i == 2*layers//3:
                C_to_auxiliary = C_prev

        if auxiliary:
            self.auxiliary_head = AuxiliaryHeadCIFAR(representative_params, C_to_auxiliary, num_classes)
        self.classifier = LinearLayer(representative_params, C_prev, num_classes)
        self.gap = nn.AdaptiveAvgPool2d((1,1))
        self.representative_params = representative_params
    def forward(self, input):
        input = Preprocess(input)
        logits_aux = None
        s0 = s1 = self.stem(input)
        for i, cell in enumerate(self.cells):
            s0, s1 = s1, cell(s0, s1, self.drop_path_prob)
            if i == 2*self._layers//3:
                if self._auxiliary and self.training:
                    logits_aux = self.auxiliary_head(s1)
        output = self.gap(s1)
        output = output.view(output.size(0), -1)
        return output


class NetworkImageNet(nn.Module):
    """ImageNet network (ported from DARTS)."""

    def __init__(self, representative_params, C, num_classes, layers, auxiliary, genotype):
        super(NetworkImageNet, self).__init__()
        self._layers = layers
        self._auxiliary = auxiliary
        self.representative_params = representative_params

        self.stem0 = nn.Sequential(
            convolutions(representative_params, in_channels=cfg.MODEL.INPUT_CHANNELS, out_channels=C//2, kernel_size=3, stride=2, padding=1),
            pseudoBatchNorm(),
            nn.ReLU(inplace=False),
            convolutions(representative_params, in_channels=C//2, out_channels=C, kernel_size=3, stride=2, padding=1),
            pseudoBatchNorm(),
        )

        self.stem1 = nn.Sequential(
            nn.ReLU(inplace=False),
            convolutions(representative_params, in_channels=C, out_channels=C, kernel_size=3, stride=2, padding=1),
            pseudoBatchNorm(),
        )

        C_prev_prev, C_prev, C_curr = C, C, C

        self.cells = nn.ModuleList()
        reduction_prev = True
        reduction_layers = [layers//3] if cfg.TASK == 'seg' else [layers//3, 2*layers//3]
        for i in range(layers):
            if i in reduction_layers:
                C_curr *= 2
                reduction = True
            else:
                reduction = False
            cell = Cell(representative_params, genotype, C_prev_prev, C_prev, C_curr, reduction, reduction_prev)
            reduction_prev = reduction
            self.cells += [cell]
            C_prev_prev, C_prev = C_prev, cell.multiplier * C_curr
            if i == 2 * layers // 3:
                C_to_auxiliary = C_prev

        if auxiliary:
            self.auxiliary_head = AuxiliaryHeadImageNet(C_to_auxiliary, num_classes)
        self.classifier = Classifier(representative_params, C_prev, num_classes)
        self.gap = nn.AdaptiveAvgPool2d((1,1))
    def forward(self, input):
        input = Preprocess(input)
        logits_aux = None
        s0 = self.stem0(input)
        s1 = self.stem1(s0)
        for i, cell in enumerate(self.cells):
            s0, s1 = s1, cell(s0, s1, self.drop_path_prob)
            if i == 2 * self._layers // 3:
                if self._auxiliary and self.training:
                    logits_aux = self.auxiliary_head(s1)

        # logits = self.classifier(s1, input.shape[2:])
        # if self._auxiliary and self.training:
        #     return logits, logits_aux
        output = self.gap(s1)
        output = output.view(output.size(0), -1)
        return output


class NAS(nn.Module):
    """NAS net wrapper (delegates to nets from DARTS)."""

    def __init__(self):
        assert cfg.TRAIN.DATASET in ['cifar10', 'imagenet', 'cityscapes'], \
            'Training on {} is not supported'.format(cfg.TRAIN.DATASET)
        assert cfg.TEST.DATASET in ['cifar10', 'imagenet', 'cityscapes'], \
            'Testing on {} is not supported'.format(cfg.TEST.DATASET)
        assert cfg.NAS.GENOTYPE in GENOTYPES, \
            'Genotype {} not supported'.format(cfg.NAS.GENOTYPE)
        super(NAS, self).__init__()
        # logger.info('Constructing NAS: {}'.format(cfg.NAS))
        # Use a custom or predefined genotype
        if cfg.NAS.GENOTYPE == 'custom':
            genotype = Genotype(
                normal=cfg.NAS.CUSTOM_GENOTYPE[0],
                normal_concat=cfg.NAS.CUSTOM_GENOTYPE[1],
                reduce=cfg.NAS.CUSTOM_GENOTYPE[2],
                reduce_concat=cfg.NAS.CUSTOM_GENOTYPE[3],
            )
        else:
            genotype = GENOTYPES[cfg.NAS.GENOTYPE]
        # Determine the network constructor for dataset
        if 'cifar' in cfg.TRAIN.DATASET:
            net_ctor = NetworkCIFAR
        else:
            net_ctor = NetworkImageNet
        # Construct the network
        self.net_ = net_ctor(
            C=cfg.NAS.WIDTH,
            num_classes=cfg.MODEL.NUM_CLASSES,
            layers=cfg.NAS.DEPTH,
            auxiliary=cfg.NAS.AUX,
            genotype=genotype
        )
        # Drop path probability (set / annealed based on epoch)
        self.net_.drop_path_prob = 0.0

    def set_drop_path_prob(self, drop_path_prob):
        self.net_.drop_path_prob = drop_path_prob

    def forward(self, x):
        return self.net_.forward(x)