#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FedMM
author: yz
0115/2024
"""

import logging
from datetime import datetime
import sys
import os
import copy
from tqdm import tqdm
import numpy as np
import random
import pandas as pd
from timeit import default_timer as timer
import matplotlib.pyplot as plt
from sklearn.metrics import classification_report, r2_score, roc_auc_score
from sklearn.datasets import load_svmlight_file
import pickle
import torch
import torchvision
from torchvision import transforms
from torch.utils.data import Dataset, TensorDataset
from sklearn.preprocessing import MinMaxScaler
from tqdm import tqdm
from typing import Dict
from typing import Any
# from load_modelnet_10 import load_modelnet_10_data
from fedmm.utilities.utils import average_models
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from pytorch_utils import CustomTensorDataset, normalize, federated_avg, get_train_or_test_loss_generic

def log_time(file, string=""):
    if string == "" :
        with open(file, 'a') as f:
            f.write(f"Started at :{timer()} \n")
    else:
        with open(file, 'a') as f:
            f.write(f"Finished {string} :{timer()} \n")

def seed_torch(seed=3407):
    random.seed(seed)
    os.environ['PYTHONHASHSEED'] = str(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed) 
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True

def cycle(iterable):
    iterator = iter(iterable)
    while True:
        try:
            yield next(iterator)
        except StopIteration:
            # print("iter here")
            iterator = iter(iterable)       
       
def samplemini(Q, BATCH_SIZE, GLOBAL_INDICES, with_replacement=True, sample1=True):
    if not sample1:
        minibatches = []
        if with_replacement:    
            for i in range(Q):
                minibatches.append(random.sample(GLOBAL_INDICES, BATCH_SIZE))
        else:
            copy_GLOBAL_INDICES = copy.deepcopy(GLOBAL_INDICES)
            random.shuffle(copy_GLOBAL_INDICES)
            start = 0
            for i in range(Q):
                minibatches.append(copy_GLOBAL_INDICES[start: (start+1)*BATCH_SIZE])
                start+=1
    else:
        minibatches = []
        sampleonce = random.sample(GLOBAL_INDICES, BATCH_SIZE)
        for i in range(Q):
            minibatches.append(sampleonce)
                
    return minibatches
      
class CD(object):
    def __init__(self, alpha: float , X, y , index: int, offset: int, device_list: list, average_network: nn.Module) -> None:
        self.alpha: float = alpha
        self.costs = []
        self.X = X
        self.y = y
        self.index = index
        self.device_list = device_list
        self.average_network = average_network
        
class Device(object):
    def __init__(self, network: nn.Module, alpha: float , X, 
                 y, device_index: int, dc_index: int, offset: int, 
                 indices : list, batch_size, transform=None, momentum=0, sampling_with_replacement=False) -> None:
        self.alpha: float = alpha
        self.momentum: float = momentum
        self.indices = indices
        self.batch_size = batch_size
        self.X = pd.DataFrame(X.reshape(X.shape[0], 3*X.shape[2]*X.shape[3]))
        self.y = pd.DataFrame(y)
        self.X.set_index(np.array(self.indices), inplace=True)
        self.y.set_index(np.array(self.indices), inplace=True)
        self.device_index = device_index
        self.dc_index = dc_index
        self.offset = offset
        self.network = network
        self.optimizer = optim.SGD(self.network.parameters(), lr=alpha,
                      momentum=self.momentum)
        self.lastlayer_Xtheta = torch.zeros((len(X), 256))
    
    def reset_optimizer(self):
        self.optimizer = optim.SGD(self.network.parameters(), lr=self.alpha,
                      momentum=self.momentum)
    
    def getBatchFromIndices(self,indices, Qindex):
        current_batch_index = indices[Qindex]
        intersected_data_points = set(current_batch_index).intersection(set(self.indices))
        return self.X.loc[intersected_data_points, :], self.y.loc[intersected_data_points, :], list(intersected_data_points)
    
def parse_args():
    import argparse
    parser = argparse.ArgumentParser(description='fedmm_yz_m40')
    parser.add_argument('--seed', type=int, nargs='?', default=3407)
    parser.add_argument('--silos', type=int, nargs='?', default=5)
    parser.add_argument('--parties', type=int, nargs='?', default=2)
    parser.add_argument('--gepochs', type=int, nargs='?', default=100)
    parser.add_argument('--Q', type=int, nargs='?', default=5)
    parser.add_argument('--R', type=int, nargs='?', default=2)
    parser.add_argument('--batchsize', type=int, nargs='?', default=160)
    parser.add_argument('--lr', type=float, nargs='?', default=0.01)
    parser.add_argument('--evalafter', type=float, nargs='?', default=5)
    parser.add_argument('--withreplacement', action='store_true')
    parser.add_argument('--momentum', type=float, nargs='?', default=0)
    parser.add_argument('--lambduh', type=float, nargs='?', default=0.01)
    parser.add_argument('--resultfolder', type=str, nargs='?', default="/a/bear.cs.fiu.edu./disk/bear-c/users/rxm1351/yz/0108fedmm/fedmm/res/mn40/")
    parser.add_argument('--stepLR', action='store_true')
    parser.add_argument('--modelnet_type', type=str, nargs='?', default="40")
    parser.add_argument('--device', type=str, nargs='?', default='cuda:0')

    args = parser.parse_args()
    # print(args)
    return args

if __name__ == "__main__":

    log_path = '/a/bear.cs.fiu.edu./disk/bear-c/users/rxm1351/yz/0108fedmm/fedmm/log/m40/'
    log_filename = f"{log_path}modelnet40_0115_log_{datetime.now().strftime('%Y%m%d%H%M%S')}.txt"
    logging.basicConfig(filename=log_filename, level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    
    args = parse_args()
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    seed_torch(args.seed)

    training_device = torch.device(args.device if torch.cuda.is_available() else 'cpu')
    print(f"Using device: {training_device}")
    logging.info(f"modelnet_40_Using device: {training_device}")
    
    WHICH_COORDINATE_INDEX_TO_DIST = 3
    if args.modelnet_type == "40":
        modelnet_10_dir = "/a/bear.cs.fiu.edu./disk/bear-c/users/rxm1351/yz/0108fedmm/TDCD/view/classes/"
    logging.info(f"Loading from {modelnet_10_dir}")

    X_train, train_filenames, y_train, X_test, test_filenames, y_test, label_names = load_modelnet_10_data(modelnet_10_dir)
    X_train = torch.FloatTensor(torch.stack(X_train))
    X_test = torch.FloatTensor(torch.stack(X_test))
    
    y_train = torch.FloatTensor(y_train)
    y_test = torch.FloatTensor(y_test)
    
    perm = np.random.permutation(len(X_train))
    X_train = X_train[perm]
    y_train = y_train[perm]

    _, CHANNELS, HEIGHT, WIDTH = X_train.shape
    
    X_train_numpy = X_train.numpy()
    y_train_numpy = y_train.numpy()

    M = args.silos 
    K = args.parties 
    global_epoch = args.gepochs 
    VFL_iter = args.Q 
    HFL_iter = args.R 
    local_batch_size = args.batchsize 
    datapoints_per_device = int(X_train.shape[0]/(K))
    alpha = args.lr 
    momentum = args.momentum
    lambduh = args.lambduh
    decreasing_step = False

    dc_list = []
    global_weights = np.zeros((X_train.shape[WHICH_COORDINATE_INDEX_TO_DIST], 1))
    global_indices = list(range(len(X_train)))
    GLOBAL_INDICES = list(range(len(X_train)))

    coordinate_partitions = []
    coordinate_per_dc = int(X_train.shape[WHICH_COORDINATE_INDEX_TO_DIST]/K)
    extradatapointsinfirstdevice = X_train.shape[WHICH_COORDINATE_INDEX_TO_DIST] - coordinate_per_dc*K
    i = 0
    while i < X_train.shape[WHICH_COORDINATE_INDEX_TO_DIST]:
        if extradatapointsinfirstdevice>0:
            coordinate_partitions.append(list(range(i, i+ coordinate_per_dc + 1)))
            extradatapointsinfirstdevice-=1
            i=i+coordinate_per_dc + 1
        else:
            coordinate_partitions.append(list(range(i, i+ coordinate_per_dc )))
            i=i+coordinate_per_dc
    
    over_train_loader = torch.utils.data.DataLoader(CustomTensorDataset(tensors=(X_train, y_train), transform=None)
                                    , batch_size=int(args.batchsize/K), shuffle=False)
    over_test_loader = torch.utils.data.DataLoader(CustomTensorDataset(tensors=(X_test, y_test), transform=None)
                                    , batch_size=int(args.batchsize/K), shuffle=False)
    
    for m in range(M):
        for k in range(K):
            coordinate_per_dc = len(coordinate_partitions[i])
            dc_X = X_train_numpy[:, :, :, coordinate_partitions[i]]
            device_list = []
            network_local = torchvision.models.resnet18() 
            for n in range(N_m):
                device_list.append(Device(alpha=alpha,
                                        momentum=momentum,
                                        X=dc_X[n*datapoints_per_device : (n+1) * datapoints_per_device, :, :, :],
                                        y=y_train_numpy[k*datapoints_per_device : (n+1) * datapoints_per_device],
                                        device_index=k,
                                        dc_index=n,
                                        offset=datapoints_per_device,
                                        indices = list(range(k*datapoints_per_device , (n+1) * datapoints_per_device)),
                                        batch_size = local_batch_size, 
                                        network = copy.deepcopy(network_local),
                                        sampling_with_replacement= args.withreplacement
                                    ))
                
        dc_list.append(CD(alpha=alpha, 
                          X=dc_X,
                          y=y_train,
                          index=i,
                          offset=coordinate_per_dc, 
                          device_list=device_list,
                          average_network = copy.deepcopy(network_local)))
        
    del X_train, y_train
    report = {"train_loss": [],
              "test_loss":[],
              "train_accuracy":[],
              "test_accuracy": [],
              "train_accuracy5":[],
              "test_accuracy5": [],
              "train_ret":[],
              "test_ret":[],
              "hyperparameters":args
              }
    
    START_EPOCH = 0
    PATH = (f"Checkpoint_Modelnet_model_BS{local_batch_size}_M{M}_K{K}_Q{VFL_iter}_R{HFL_iter}_lr{alpha}_momentum{momentum}_seed{args.seed}_sampling{args.withreplacement}_modelnet{args.modelnet_type}.pt")
    if os.path.exists(PATH):
        checkpoint = torch.load(PATH)
        START_EPOCH = int(checkpoint['epoch']) + 1 
        for hub_idx in range(M):
            dc_list[hub_idx].average_network.load_state_dict(checkpoint["hub_average_network_state_dict"][hub_idx])
            for device_idx, device in enumerate(dc_list[hub_idx].device_list):
                device.network = copy.deepcopy(dc_list[hub_idx].average_network)
                device.reset_optimizer()

        if not args.stepLR:
            filename =f"Modelnet_model_BS{local_batch_size}_M{M}_K{K}_Q{VFL_iter}_R{HFL_iter}_lr{alpha}_momentum{momentum}_seed{args.seed}_sampling{args.withreplacement}_eval{args.evaluateateveryiteration}_evalafter{args.evalafter}_modelnet{args.modelnet_type}.pkl" 
        else:
            filename =f"Modelnet_model_BS{local_batch_size}_M{M}_K{K}_Q{VFL_iter}_R{HFL_iter}_lr[{alpha},0.005,0.001]_momentum{momentum}_seed{args.seed}_sampling{args.withreplacement}_eval{args.evaluateateveryiteration}_evalafter{args.evalafter}_modelnet{args.modelnet_type}.pkl" 
        
        f = open(os.path.join(args.resultfolder, filename), "rb")
        report = pickle.load(f)

    for t in range(START_EPOCH, global_epoch):
        # print(f"Epoch {t}/{global_epoch}")
        logging.info(f"Epoch {t}/{global_epoch}")
        batch_for_round = {}
        batch_indices_and_exchange_info_for_epoch = {i:{} for i in range(K)}
        mini_batch_indices = samplemini(VFL_iter, HFL_iter, args.batchsize, GLOBAL_INDICES, with_replacement=True)
        for party_index, k in enumerate(range(K)):
            current_DC = dc_list[party_index]
            coordinate_per_dc = len(coordinate_partitions[k])
            for device_idx, device in enumerate(current_DC.device_list):
                batch_indices_and_exchange_info_for_epoch[party_index][device.device_index] = []
                for iterations in range(HFL_iter):
                    for iterations in range(VFL_iter):
                        temp_X , temp_y, batch_indices = device.getBatchFromIndices(mini_batch_indices, iterations)
                        device.network.to(training_device)
                        with torch.no_grad():
                            if len(temp_X)==0:
                                batch_indices_and_exchange_info_for_epoch[party_index][device.device_index].append({"batch_indices": copy.deepcopy(batch_indices), "embedding":torch.zeros(1)})
                                continue
                            temp_X = torch.FloatTensor(np.array(temp_X).reshape(temp_X.shape[0], CHANNELS, HEIGHT, coordinate_per_dc))
                            temp_X = temp_X.to(training_device)
                            output = device.network(temp_X)
                            batch_indices_and_exchange_info_for_epoch[party_index][device.device_index]\
                                .append({"batch_indices": copy.deepcopy(batch_indices), "embedding":output})
            
            for party_index, k in enumerate(range(K)):
                coordinate_per_dc = len(coordinate_partitions[k])
                current_DC = dc_list[party_index]

                for device_idx, device in enumerate(current_DC.device_list):
                    device.network.to(training_device)
                    device.network.train()
                    batch_indices = batch_indices_and_exchange_info_for_epoch[party_index][device_idx][iteration]["batch_indices"]
                    temp_X , temp_y, _ = device.getBatchFromIndices(mini_batch_indices, iteration)
                    temp_X = torch.FloatTensor(np.array(temp_X).reshape(temp_X.shape[0], CHANNELS, HEIGHT, coordinate_per_dc))
                    temp_y = torch.FloatTensor(np.array(temp_y))[:,0]
                    temp_X , temp_y = temp_X.to(training_device), temp_y.to(training_device)
                    device.optimizer.zero_grad()
                    output = device.network(temp_X)
                    output_top_from_other_hub_client = []
                    for dc_index in range(K):
                        if dc_index == party_index:
                            continue
                        else:
                            output_top_from_other_hub_client.append(batch_indices_and_exchange_info_for_epoch[dc_index][device.device_index][iteration]["embedding"])

                    if len(output_top_from_other_hub_client)>0:
                        output_top_from_other_hub_client = torch.stack(output_top_from_other_hub_client, dim=0).sum(dim=0)
                    output_top = output

                    if K>1:
                        total_output = output_top+output_top_from_other_hub_client
                    else:
                        total_output = output_top
                    loss = F.cross_entropy(total_output, temp_y.long())
                    loss.backward()
                    device.optimizer.step()
   
        for party_index, k in enumerate(range(K)):
            current_DC = dc_list[party_index]
            device_model_list = {}
            for device_idx, device in enumerate(current_DC.device_list):
                device_model_list[device_idx] = copy.deepcopy(device.network) 
            current_DC.average_network = federated_avg(device_model_list)
            for device_idx, device in enumerate(current_DC.device_list):
                device.network = copy.deepcopy(current_DC.average_network)
                device.reset_optimizer()

        PATH = (f"Checkpoint_BS{local_batch_size}_M{M}_K{K}_Q{VFL_iter}_R{HFL_iter}_lr{alpha}_momentum{momentum}_seed{args.seed}_sampling{args.withreplacement}_modelnet{args.modelnet_type}.pt")
        torch.save({
            'epoch': t,
            'average_network' : [i.average_network.state_dict() for i in dc_list],
        }, PATH)
        os.makedirs(f"{args.resultfolder}", exist_ok=True)
        if not args.stepLR:
            filename =f"BS{local_batch_size}_N{N}_K{K}_Q{VFL_iter}_R{HFL_iter}_lr{alpha}_momentum{momentum}_seed{args.seed}_sampling{args.withreplacement}_eval{args.evaluateateveryiteration}_evalafter{args.evalafter}_modelnet{args.modelnet_type}.pkl" 
        else:
            filename =f"BS{local_batch_size}_N{N}_K{K}_Q{VFL_iter}_R{HFL_iter}_lr[{alpha},0.005,0.001]_momentum{momentum}_seed{args.seed}_sampling{args.withreplacement}_eval{args.evaluateateveryiteration}_evalafter{args.evalafter}_modelnet{args.modelnet_type}.pkl" 
        f = open(os.path.join(args.resultfolder, filename), "wb")
        pickle.dump(report, f)
