from typing import OrderedDict
from numpy.lib.function_base import select
import torch
import logging
import val
import torch.nn as nn
import numpy as np
from utils.metrics import fitness
from utils.general import colorstr
from utils.torch_utils import is_parallel
from utils.datasets import create_dataloader
from prettytable import PrettyTable
from copy import deepcopy


logger = logging.getLogger(__name__)
# Soft Filters Pruning

def setup_seed(seed):
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    np.random.seed(seed)
    torch.backends.cudnn.deterministic = True
    
def get_skip_list(model, skip_list):
    layer = OrderedDict()
    for i, (name, _) in enumerate(model.named_parameters()):
        if name.endswith('.bn.weight'):
            layer[i - 1] = name.replace('.weight', '')
    skip_list = [layer[k] for k in skip_list]
    
    return skip_list
class Mask:
    def __init__(self, 
                 model, 
                 device, 
                 opt=None, 
                 **kwargs):
        self.cuda = device.type != 'cpu' 
        self.device = device
        self.model_size = {}
        self.model_length = {}
        self.compress_rate = {}
        self.mat = {}
        self.model = model.module if is_parallel(model) else model
        self.mask_index = []
        self.opt = opt if opt is not None else kwargs
        self.layer_begin, self.layer_end, self.layer_inter = [
            int(i.strip(' ')) for i in opt.layer_gap.split(',')
        ]
    def init_length(self):
        for index, item in enumerate(self.model.parameters()):
            self.model_size[index] = item.size()
            self.model_length[index] = np.prod(self.model_size[index])

    def init_rate(self, layer_rate):
        for index, item in enumerate(self.model.parameters()):
            self.compress_rate[index] = 1
        for key in range(self.layer_begin, self.layer_end, self.layer_inter):
            self.compress_rate[key] = layer_rate
        last_index = 321
        skip_list = [0,3, 
        6,15,21,27,
        36,45,51,57,63,69,75,81,87,93,
        102,111,117,123,129,135,141,147,153,159,
        174,183,189,195,
        258,288,318]
        # self.opt.skip_list 
        # [x for x in range(0, last_index, 3) if x not in range(0, 201, 3)]

        # [x for x in range(0, last_index, 3) if x not in range(0, 33, 3)]
        self.mask_index = [x for x in range(0, last_index, 3)]
        if self.opt.skip_downsample:
            for x in skip_list:
                self.compress_rate[x] = 1
                self.mask_index.remove(x)

    def init_mask(self, layer_rate, print_info=True):
        self.init_rate(layer_rate)
        if print_info:
            prefix = colorstr('mask_index:')
            logging.info('{}\n{}'.format(prefix, self.mask_index))

        for index, item in enumerate(self.model.parameters()):
            if (index in self.mask_index):
                self.mat[index] = self.get_filter_codebook(item.data, self.compress_rate[index], 
                                                           self.model_length[index])
                self.mat[index] = self.convert2tensor(self.mat[index])
                if self.cuda:
                    self.mat[index] = self.mat[index].to(self.device)

        logging.info('Mask Ready...')

    def do_mask(self):
        for index, item in enumerate(self.model.parameters()):
            if (index in self.mask_index):
                a = item.data.view(self.model_length[index])
                b = a * self.mat[index]
                item.data = b.view(self.model_size[index])
        logging.info('Mask Done...')
        
    def get_filter_codebook(self, weight_torch, compress_rate, length):
        codebook = np.ones(length)
        if len(weight_torch.size()) == 4:
            filter_pruned_num = int(weight_torch.size()[0] * (1 - compress_rate)) 
            weight_vec = weight_torch.view(weight_torch.size()[0], -1) # view不是inplace，重新复制了一份
            norm2 = torch.norm(weight_vec, 2, 1) 
            norm2_np = norm2.cpu().numpy()
            filter_index = norm2_np.argsort()[ :filter_pruned_num]
            kernel_length = np.prod(weight_torch.size()[1: ])
            for x in range(0, len(filter_index)):
                codebook[filter_index[x] * kernel_length: (filter_index[x] + 1) * kernel_length] = 0

            return codebook

    def convert2tensor(self, x):
        x = torch.FloatTensor(x) 
        return x

    # def get_codebook(self, weight_torch, compress_rate, length):
    #     weight_vec = weight_torch.view(length)
    #     weight_np = weight_vec.cpu().numpy()

    #     weight_abs = np.abs(weight_np)
    #     weight_sort = np.sort(weight_abs)

    #     threshold = weight_sort[int(length * (1 - compress_rate))]
    #     weight_np[weight_np <= -threshold] = 1
    #     weight_np[weight_np >= threshold] = 1
    #     weight_np[weight_np != 1] = 0

    #     return weight_np

    def if_zero(self, epoch=None, save_file=None):
        prefix_print = True
        for index, item in enumerate(self.model.parameters()):
            if index in [x for x in range(self.layer_begin, self.layer_end, self.layer_inter)]:
                a = item.data.view(self.model_length[index])
                b = a.cpu().numpy()
                if save_file is not None:
                    assert save_file.match('*.txt'), 'the prune save must be txt'
                    with open(save_file, 'a') as fw:
                        if epoch is not None and prefix_print:
                            prefix = '>' * 20 + f' epoch:{epoch} ' + '<' * 20 + '\n'
                            fw.write(prefix)
                            prefix_print = False
                        fw.write('layer:{}, number of nonzero weight is {}, zero is {}\n'.format(index, 
                                  np.count_nonzero(b), len(b) - np.count_nonzero(b)))
                else:
                    prefix = colorstr('layer:')
                    logging.info('{}{}, number of nonzero weight is {}, zero is {}'.format(prefix, 
                                index, np.count_nonzero(b), len(b) - np.count_nonzero(b)))


def get_pruning_cfg(cfg):
    new_cfg = OrderedDict()
    for k, v in cfg.items():
        for cfg in v:
            if isinstance(cfg, list):
                cfg = tuple(cfg)
            if isinstance(k, str):
                k = int(k)
            if k in new_cfg:
                new_cfg[k] += [cfg]
            else:
                new_cfg[k] = [cfg]
    logging.info('the pruning configuration convertion is completed!')
    return new_cfg

                
# Network Slimming
class BNOptimizer():
    def __init__(self, model, opt, **kwargs):
        self.model = model.module if is_parallel(model) else model
        self.opt = opt

    def updateBN(self):
        for _, (name, module) in enumerate(self.model.named_modules()):
            if isinstance(module, nn.BatchNorm2d):
                if name not in get_skip_list(self.model, self.opt.skip_list):
                    module.weight.grad.data.add_(self.opt.s * torch.sign(module.weight.data))
        prefix = colorstr('Update BN is completed!')
        logger.info(prefix)

def gather_bn_weights(model, skip_list):
    size_list = []
    module_weights = []
    highest_thre = []
    for _, (name, module) in enumerate(model.named_modules()):
        if isinstance(module, nn.BatchNorm2d):
            if name not in get_skip_list(model, skip_list):
                size_list.append(module.weight.data.shape[0])
                module_weights.append(module.weight.data.abs().clone())
                highest_thre.append(module.weight.data.abs().max().item())
    bn_weights = torch.zeros(sum(size_list))
    index = 0
    for size, weight in zip(size_list, module_weights):
        bn_weights[index: (index + size)] = weight.view(-1)
        index += size
    
    return [bn_weights, min(highest_thre)]

def get_sparse_model(model, skip_list, ratio, sorted_bn):
    pruned_percent = 1 - ratio
    thre = sorted_bn[int(len(sorted_bn) * pruned_percent)]
    logger.info(f'the ratio is {ratio}, the thre is {thre}')
    conv_mask = OrderedDict()
    for name, module in model.named_modules():
        if isinstance(module, nn.BatchNorm2d):
            if name not in get_skip_list(model, skip_list):
                mask = module.weight.data.abs().gt(thre).float()
                conv_name = name.replace('bn', 'conv')
                conv_mask[conv_name] = mask.view(-1, 1, 1, 1)
    for name, module in model.named_modules():
        if isinstance(module, nn.Conv2d):
            if name in conv_mask:
                module.weight.data.mul_(conv_mask[name]) 

def get_mask(select_index, shape):
    mask = torch.ones(shape)
    mask[select_index] = 0
    return mask

def model_eval(model, data_dict, device=None):
    gs = max(int(model.stride.max()), 32)
    valloader = create_dataloader(data_dict['val'], 640, 1, gs, 
                                  workers=1, pad=0.5, rect=True,
                                  prefix=colorstr('val: '))[0]
    # TODO:释放deepcopy的内存空间
    with torch.no_grad():
        results, _, _ = val.run(data_dict,
                                batch_size = 1, 
                                imgsz = 640,
                                model = deepcopy(model).to(device) if device else model,
                                plots = False,
                                dataloader = valloader)
    fi = fitness(np.array(results).reshape(1, -1))
    prefix = colorstr('val results:')
    logger.info(f"{prefix}{fi}")

def model_compare(model, model_pruning, mode = 'parameters'):

    logger.info('begin printing different of each layer...')
    prefix = colorstr(f"{mode}")
    # 将每一层输出的feature maps的L1 norm打印
    if mode == 'parameters':
        table = PrettyTable([colorstr('layer'),
                             colorstr('name'), 
                             colorstr('unpruning'), 
                             colorstr('pruning'), 
                             colorstr('distance')])
        ignore = ['bn', 'running_mean', 'running_var', 'num_batches_tracked']
        for i, (c1, c2) in enumerate(zip(model.state_dict().items(), model_pruning.state_dict().items())):
            if not any([k in c1[0] for k in ignore]):
                p1_total = torch.norm(c1[1])
                p2_total = torch.norm(c2[1])
                distance = p1_total - p2_total
                table.add_row([i, 
                            c1[0],
                            round(p1_total.item(), 5),
                            round(p2_total.item(), 5), 
                            round(distance.item(), 5)])
    elif mode == 'features':
        table = PrettyTable([colorstr('layer'),
                             colorstr('unpruning'), 
                             colorstr('pruning'), 
                             colorstr('distance')])
        model.eval()
        model_pruning.eval()
        input = torch.randn((1, 3, 640, 640))
        output1 = model(input, features=True)
        output2 = model_pruning(input, features=True)
        for i, (o1, o2) in enumerate(zip(output1, output2)):
            if isinstance(o1, tuple) or isinstance(o2, tuple):
                o1, o2 = o1[0], o2[0]
            o1_norm = torch.norm(o1)
            o2_norm = torch.norm(o2)
            distance = o1_norm - o2_norm
            table.add_row([i,
                           round(o1_norm.item(), 5),
                           round(o2_norm.item(), 5),
                           round(distance.item(), 5)])
    logger.info(f"the different of {prefix}:")
    logger.info(table)


        

