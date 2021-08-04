import sys
from typing import OrderedDict
import torch
import numpy as np
import argparse
import logging
import yaml
import json
from models.yolo import Model as Model
from models.yolov5l_pruning import Model as Model_Pruning
from pathlib import Path
FILE = Path(__file__).absolute()
sys.path.append(FILE.parents[0].as_posix())

logger = logging.getLogger(__name__)

def main(opt):
    setup_seed(20)
    weights, cfg = opt.weights, opt.cfg
    # Load the model
    weights = Path(weights)
    pruning_weights = weights.parents[0] / ('pruning_' + weights.name)
    pruning_cfg = weights.parents[0] / 'pruning_cfg.json'
    cfg = Path(cfg)
    assert weights.match('*.pt'), 'the file must be the type of *.pt '
    ckpt = torch.load(weights, map_location=lambda storage, loc: storage) # 将权重从gpu上加载进cpu中
    model = ckpt['model'].float()
 
    # Load the model configuration
    assert cfg.match('*.yaml'), 'the file must be the type of *.yaml'
    with open(cfg) as f:
        cfg = yaml.safe_load(f)
    cfg['nc'] = 1
    input_from = OrderedDict()
    shortcut = OrderedDict()
    for i, m in enumerate(cfg['backbone'] + cfg['head']):
        input_from[i] = m[0]
        shortcut[i] = m[3][-1]

    # Pruning the params
    first_layer = 0
    last_layer = 641
    inter_layer = 6
    layer_index = [i for i in range(first_layer, last_layer + 1, inter_layer)]
    channel_index = OrderedDict()
    state_dict_ = OrderedDict()
    keep_mask = []
    state_dict_strip = strip_params(model)
    # prune the output channels params
    
    for i, (n, param) in enumerate(state_dict_strip.items()):
        m_index = int(n.split('.')[0])
        if i in layer_index: # 获得保留权重的通道和去除权重通道的index
            if shortcut[m_index] == True and 'cv1' in n and 'm' not in n:  
                keep_index = torch.LongTensor(range(param.shape[0]))
            else:
                keep_index = torch.nonzero(torch.sum(param.data, dim=(1, 2, 3)) != 0).view(-1) 
            state_dict_[n] = param[keep_index].clone()

            keep_mask.append((param.shape, keep_index))
            if m_index in channel_index:
                channel_index[m_index] += [(param.shape, keep_index)]
            else:
                channel_index[m_index] = [(param.shape, keep_index)]
 
        elif (i not in layer_index) and (first_layer < i < last_layer):
            if len(param.shape) != 0:
                state_dict_[n] = param[keep_mask[-1][-1]].clone()
            else:
                state_dict_[n] = param.clone()
        else:
            state_dict_[n] = param.clone()
    del state_dict_strip
    # prune the input channels params
    index_ = -1
    for i, (n, param) in enumerate(state_dict_.items()):
        m_index = int(n.split('.')[0])
        if i in layer_index:
            index_ += 1
            if index_ == 0:
                continue
            if  'm' not in n:
                if 'cv1' in n:
                    f = input_from[m_index - 1]
                    if isinstance(f, list):
                        k1 = keep_mask[index_ - 1][-1]
                        k2 = channel_index[f[-1]][-1][-1] + keep_mask[index_ - 1][0][0]
                        k = torch.cat((k1, k2), dim=0)
                    else:
                        k = keep_mask[index_ - 1][-1]
                    state_dict_[n] = param[:, k].clone()
                    continue
                elif ('cv2' in n) and (m_index == 8):
                    k = torch.cat([(keep_mask[index_ - 1][-1] + d * keep_mask[index_ - 1][0][0]) for d in range(4)])
                    state_dict_[n] = param[:, k].clone()
                    continue
                elif ('cv2' in n) and (m_index != 8):
                    f = input_from[m_index - 1]
                    if isinstance(f, list):
                        k1 = keep_mask[index_ - 2][-1]
                        k2 = channel_index[f[-1]][-1][-1] + keep_mask[index_ - 2][0][0]
                        k = torch.cat((k1, k2), dim=0)
                    else:
                        k = keep_mask[index_ - 2][-1]
                    state_dict_[n] = param[:, k].clone()
                    continue
                elif 'cv3' in n:
                    if shortcut[m_index] == True:
                        k1 = torch.LongTensor(range(keep_mask[index_ - 1][0][0]))
                    else:
                        k1 = keep_mask[index_ - 1][-1]
                    k2 = channel_index[m_index][1][-1] + keep_mask[index_ - 1][0][0]
                    k = torch.cat((k1, k2), dim=0)
                    state_dict_[n] = param[:, k].clone()
                    continue
            if shortcut[m_index] == True and 'cv1' in n and 'm' in n:
               k = torch.LongTensor(range(keep_mask[index_ - (2 if 'm.0' in n else 1)][0][0]))
            else:
                k = keep_mask[index_ - (2 if 'm.0' in n else 1)][-1]
            state_dict_[n] = param[:, k].clone()
        elif i > last_layer:
            if 'anchor' not in n:
                f = input_from[m_index]
                if 'weight' in n:
                    k = channel_index[f[int((i - last_layer - 3) / 2)]][-1][-1]
                    state_dict_[n] = param[:, k].clone()

    channel_index = OrderedDict()
    index_ = -1
    for i, (n, param) in enumerate(state_dict_.items()):
        m_index = int(n.split('.')[0])
        shape = list(param.shape[: 2])
        if i in layer_index:
            index_ += 1
            if m_index in channel_index:
                channel_index[m_index] += [(shape, keep_mask[index_][-1].tolist())]
            else:
                channel_index[m_index] = [(shape, keep_mask[index_][-1].tolist())]
        elif i > last_layer:
            if 'anchor' not in n:
                if 'weight' in n:
                    if m_index in channel_index:
                        channel_index[m_index] += [shape[1]]
                    else:
                        channel_index[m_index] = [shape[1]]

    with open(pruning_cfg, 'w') as f:
        f.write(json.dumps(channel_index))

    model_pruning = Model_Pruning(cfg, channel_index=channel_index)
       
    new_state_dict = OrderedDict()
    for i, (n1, n2) in enumerate(zip(state_dict_.items(), model_pruning.state_dict().items())):
        assert n1[-1].shape == n2[-1].shape, 'there are errors in state_dict_'
        new_state_dict[n2[0]] = state_dict_[n1[0]]
    model_pruning.load_state_dict(new_state_dict, strict=True)
    # compare
    model.eval()
    model_pruning.eval()

    with torch.no_grad():
        input = torch.randn((1, 3, 640, 640))
        output = model(input, features=True)
        output_ = model_pruning(input, features=True)
        for i, (o1, o2) in enumerate(zip(output, output_)):
            if isinstance(o1, tuple) or isinstance(o2, tuple):
                o1 = o1[0]
                o2 = o2[0]
            different = torch.norm(o1) - torch.norm(o2)
            print(f"the different of the {i}th is {different} | {torch.norm(o1)} - {torch.norm(o2)}")
    
    # save model
    ckpt['model'] = model_pruning.half()
    torch.save(ckpt, pruning_weights)

def strip_params(model):
    state_dict_ = OrderedDict()
    for i, (n, p) in enumerate(model.state_dict().items()):
        assert n.startswith('model.'), 'please check the modules of the model'
        state_dict_[n.lstrip('model.')] = p

    return state_dict_

def setup_seed(seed):
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    np.random.seed(seed)
    torch.backends.cudnn.deterministic = True

def get_opt():
    parser = argparse.ArgumentParser()
    parser.add_argument('--weights', type=str, default='weights/yolov5l.pt')
    parser.add_argument('--cfg', type=str, default='models/yolov5l.yaml')
    opt = parser.parse_args()
    return opt

if __name__ == '__main__':
    opt = get_opt()
    main(opt)