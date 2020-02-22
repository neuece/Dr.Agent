<<<<<<< HEAD
import numpy as np
import argparse
import os
import imp
import re
import pickle
import random
import matplotlib.pyplot as plt
import matplotlib as mpl

RANDOM_SEED = 12345
np.random.seed(RANDOM_SEED)
random.seed(RANDOM_SEED)

import torch
from torch import nn
import torch.nn.utils.rnn as rnn_utils
from torch.utils import data
from torch.autograd import Variable
import torch.nn.functional as F

torch.manual_seed(RANDOM_SEED)
torch.cuda.manual_seed(RANDOM_SEED)
torch.backends.cudnn.deterministic=True

from utils.decompensation import utils
from utils.decompensation.readers import DecompensationReader
from utils.decompensation.preprocessing import Discretizer, Normalizer
from utils.decompensation import metrics
from utils.decompensation import common_utils
from model import model_decomp

def parse_arguments(parser):
    parser.add_argument('--data_path', type=str, metavar='<data_path>', help='The path to the MIMIC-III data directory')
    parser.add_argument('--save_path', type=str, metavar='<data_path>', help='The path to save the model')
    parser.add_argument('--small_part', type=int, default=0, help='Use part of training data')
    parser.add_argument('--batch_size', type=int, default=128, help='Training batch size')
    parser.add_argument('--epochs', type=int, default=50, help='Training epochs')
    parser.add_argument('--lr', type=float, default=0.001, help='Learing rate')

    parser.add_argument('--cell', type=str, default='gru', help='RNN cell')
    parser.add_argument('--input_dim', type=int, default=76, help='Dimension of visit record data')
    parser.add_argument('--demo_dim', type=int, default=12, help='Dimension of demographic data')
    parser.add_argument('--rnn_dim', type=int, default=128, help='Dimension of hidden units in RNN')
    parser.add_argument('--agent_dim', type=int, default=128, help='Dimension of MLP in two agents')
    parser.add_argument('--mlp_dim', type=int, default=72, help='Dimension of MLP for output')
    parser.add_argument('--output_dim', type=int, default=1, help='Dimension of prediction target')
    parser.add_argument('--dropout_rate', type=float, default=0.5, help='Dropout rate')
    parser.add_argument('--lamda', type=float, default=0.5, help='Value of hyper-parameter lamda')
    parser.add_argument('--K', type=int, default=10, help='Value of hyper-parameter K')
    parser.add_argument('--gamma', type=float, default=0.9, help='Value of hyper-parameter gamma')
    parser.add_argument('--entropy_term', type=float, default=0.01, help='Value of reward entropy term')

    args = parser.parse_args()
    return args

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    args = parse_arguments(parser)

    ''' Prepare training data'''
    print('Preparing training data ... ')
    train_data_loader = common_utils.DeepSupervisionDataLoader(dataset_dir=os.path.join(
        args.data_path, 'train'), listfile=os.path.join(args.data_path, 'train_listfile.csv'), small_part=args.small_part)
    val_data_loader = common_utils.DeepSupervisionDataLoader(dataset_dir=os.path.join(
        args.data_path, 'train'), listfile=os.path.join(args.data_path, 'val_listfile.csv'), small_part=args.small_part)
    discretizer = Discretizer(timestep=1.0, store_masks=True,
                            impute_strategy='previous', start_time='zero')

    discretizer_header = discretizer.transform(train_data_loader._data["X"][0])[1].split(',')
    cont_channels = [i for (i, x) in enumerate(discretizer_header) if x.find("->") == -1]

    normalizer = Normalizer(fields=cont_channels)
    normalizer_state = 'decomp_normalizer'
    normalizer_state = os.path.join(os.path.dirname(args.data_path), normalizer_state)
    normalizer.load_params(normalizer_state)

    train_data_gen = utils.BatchGenDeepSupervision(train_data_loader, discretizer,
                                                    normalizer, args.batch_size, shuffle=True, return_names=True)
    val_data_gen = utils.BatchGenDeepSupervision(val_data_loader, discretizer,
                                                normalizer, args.batch_size, shuffle=False, return_names=True)

    demographic_data = []
    diagnosis_data = []
    idx_list = []

    demo_path = args.data_path + 'demographic/'
    for cur_name in os.listdir(demo_path):
        cur_id, cur_episode = cur_name.split('_', 1)
        cur_episode = cur_episode[:-4]
        cur_file = demo_path + cur_name

        with open(cur_file, "r") as tsfile:
            header = tsfile.readline().strip().split(',')
            if header[0] != "Icustay":
                continue
            cur_data = tsfile.readline().strip().split(',')
            
        if len(cur_data) == 1:
            cur_demo = np.zeros(12)
            cur_diag = np.zeros(128)
        else:
            if cur_data[3] == '':
                cur_data[3] = 60.0
            if cur_data[4] == '':
                cur_data[4] = 160
            if cur_data[5] == '':
                cur_data[5] = 60

            cur_demo = np.zeros(12)
            cur_demo[int(cur_data[1])] = 1
            cur_demo[5 + int(cur_data[2])] = 1
            cur_demo[9:] = cur_data[3:6]
            cur_diag = np.array(cur_data[8:], dtype=np.int)

        demographic_data.append(cur_demo)
        diagnosis_data.append(cur_diag)
        idx_list.append(cur_id+'_'+cur_episode)

    for each_idx in range(9,12):
        cur_val = []
        for i in range(len(demographic_data)):
            cur_val.append(demographic_data[i][each_idx])
        cur_val = np.array(cur_val)
        _mean = np.mean(cur_val)
        _std = np.std(cur_val)
        _std = _std if _std > 1e-7 else 1e-7
        for i in range(len(demographic_data)):
            demographic_data[i][each_idx] = (demographic_data[i][each_idx] - _mean) / _std


    '''Model structure'''
    print('Constructing model ... ')
    device = torch.device("cuda:0" if torch.cuda.is_available() == True else 'cpu')
    print("available device: {}".format(device))

    model = model_decomp.Agent(cell=args.cell,
                        use_baseline=True,
                        n_actions=args.K,
                        n_units=args.agent_dim,
                        n_input=args.input_dim,
                        demo_dim=args.demo_dim,
                        fusion_dim=args.mlp_dim,
                        n_hidden=args.rnn_dim,
                        n_output=args.output_dim,
                        dropout=args.dropout_rate,
                        lamda=args.lamda, device=device).to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=args.lr)

    '''Train phase'''
    print('Start training ... ')

    train_loss = []
    val_loss = []
    batch_loss = []
    max_auroc = 0

    file_name = args.save_path+'/agent_decomp'
    for each_chunk in range(args.epochs):
        cur_batch_loss = []
        #train_data_gen.steps
        model.train()
        for each_batch in range(train_data_gen.steps):
            batch_data = next(train_data_gen)
            batch_name = batch_data['names']
            batch_data = batch_data['data']
            
            batch_demo = []
            for i in range(len(batch_name)):
                cur_id, cur_ep, _ = batch_name[i].split('_', 2)
                cur_idx = cur_id + '_' + cur_ep
                cur_demo = torch.tensor(demographic_data[idx_list.index(cur_idx)], dtype=torch.float32)
                batch_demo.append(cur_demo)
            
            batch_demo = torch.stack(batch_demo).to(device)
            batch_x = torch.tensor(batch_data[0][0], dtype=torch.float32).to(device)
            batch_mask = torch.tensor(batch_data[0][1], dtype=torch.float32).unsqueeze(-1).to(device)
            batch_y = torch.tensor(batch_data[1], dtype=torch.float32).to(device)
            
            if batch_mask.size()[1] > 400:
                batch_x = batch_x[:, :400, :]
                batch_mask = batch_mask[:, :400, :]
                batch_y = batch_y[:, :400, :]
            optimizer.zero_grad()
            cur_output = model(batch_x, batch_demo) #B T 1
            loss, loss_rl, loss_task = model_decomp.get_loss(model, cur_output, batch_y, batch_mask, gamma=args.gamma, entropy_term=args.entropy_term, use_baseline=True, device=device)
            
            cur_batch_loss.append(loss.cpu().detach().numpy())

            loss.backward()
            optimizer.step()
            
            if each_batch % 500 == 0:
                print('Chunk %d, Batch %d: Loss = %.4f Task loss = %.4f RL loss = %.4f'%(each_chunk, each_batch, cur_batch_loss[-1], loss_task, loss_rl))

        batch_loss.append(cur_batch_loss)
        train_loss.append(np.mean(np.array(cur_batch_loss)))
        
        print("\n==>Predicting on validation")
        with torch.no_grad():
            model.eval()
            cur_val_loss = []
            cur_val_rl_loss = []
            cur_val_task_loss = []
            valid_true = []
            valid_pred = []
            for each_batch in range(val_data_gen.steps):
                valid_data = next(val_data_gen)
                valid_name = valid_data['names']
                valid_data = valid_data['data']
                
                valid_demo = []
                for i in range(len(valid_name)):
                    cur_id, cur_ep, _ = valid_name[i].split('_', 2)
                    cur_idx = cur_id + '_' + cur_ep
                    cur_demo = torch.tensor(demographic_data[idx_list.index(cur_idx)], dtype=torch.float32)
                    valid_demo.append(cur_demo)
                
                valid_demo = torch.stack(valid_demo).to(device)
                valid_x = torch.tensor(valid_data[0][0], dtype=torch.float32).to(device)
                valid_mask = torch.tensor(valid_data[0][1], dtype=torch.float32).unsqueeze(-1).to(device)
                valid_y = torch.tensor(valid_data[1], dtype=torch.float32).to(device)

                if valid_mask.size()[1] > 400:
                    valid_x = valid_x[:, :400, :]
                    valid_mask = valid_mask[:, :400, :]
                    valid_y = valid_y[:, :400, :]
                
                valid_output = model(valid_x, valid_demo)
                valid_loss, valid_loss_RL, valid_loss_task = model_decomp.get_loss(model, valid_output, valid_y, valid_mask, gamma=args.gamma, entropy_term=args.entropy_term, use_baseline=True, device=device)
                
                cur_val_loss.append(valid_loss.cpu().detach().numpy())
                cur_val_rl_loss.append(valid_loss_RL.cpu().detach().numpy())
                cur_val_task_loss.append(valid_loss_task.cpu().detach().numpy())

                for m, t, p in zip(valid_mask.cpu().numpy().flatten(), valid_y.cpu().numpy().flatten(), valid_output.cpu().detach().numpy().flatten()):
                    if np.equal(m, 1):
                        valid_true.append(t)
                        valid_pred.append(p)

            val_loss.append(np.mean(np.array(cur_val_loss)))
            print('Valid loss = %.4f Task loss = %.4f RL loss = %.4f'%(val_loss[-1], np.mean(np.array(cur_val_task_loss)), np.mean(np.array(cur_val_rl_loss))))
            print('\n')
            valid_pred = np.array(valid_pred)
            valid_pred = np.stack([1 - valid_pred, valid_pred], axis=1)
            ret = metrics.print_metrics_binary(valid_true, valid_pred)
            print()

            cur_auroc = ret['auroc']
            if cur_auroc > max_auroc:
                max_auroc = cur_auroc
                state = {
                    'net': model.state_dict(),
                    'optimizer': optimizer.state_dict(),
                    'chunk': each_chunk
                }
                torch.save(state, file_name)
                print('\n------------ Save best model ------------\n')


    '''Evaluate phase'''
    print('Testing model ... ')

    checkpoint = torch.load(file_name)
    save_chunk = checkpoint['chunk']
    print("last saved model is in chunk {}".format(save_chunk))
    model.load_state_dict(checkpoint['net'])
    optimizer.load_state_dict(checkpoint['optimizer'])
    model.eval()

    test_data_loader = common_utils.DeepSupervisionDataLoader(dataset_dir=os.path.join(args.data_path, 'test'),
                                                                    listfile=os.path.join(args.data_path, 'test_listfile.csv'), small_part=args.small_part)
    test_data_gen = utils.BatchGenDeepSupervision(test_data_loader, discretizer,
                                                normalizer, args.batch_size,
                                                shuffle=False, return_names=True)

    with torch.no_grad():
        torch.manual_seed(RANDOM_SEED)
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(RANDOM_SEED)
    
        cur_test_loss = []
        test_true = []
        test_pred = []
        
        for each_batch in range(test_data_gen.steps):
            test_data = next(test_data_gen)
            test_name = test_data['names']
            test_data = test_data['data']

            test_demo = []
            for i in range(len(test_name)):
                cur_id, cur_ep, _ = test_name[i].split('_', 2)
                cur_idx = cur_id + '_' + cur_ep
                cur_demo = torch.tensor(demographic_data[idx_list.index(cur_idx)], dtype=torch.float32)
                test_demo.append(cur_demo)

            test_demo = torch.stack(test_demo).to(device)
            test_x = torch.tensor(test_data[0][0], dtype=torch.float32).to(device)
            test_mask = torch.tensor(test_data[0][1], dtype=torch.float32).unsqueeze(-1).to(device)
            test_y = torch.tensor(test_data[1], dtype=torch.float32).to(device)
            
            if test_mask.size()[1] > 400:
                test_x = test_x[:, :400, :]
                test_mask = test_mask[:, :400, :]
                test_y = test_y[:, :400, :]
            
            test_output = model(test_x, test_demo)
            test_loss, _, _ = model_decomp.get_loss(model, test_output, test_y, test_mask, gamma=args.gamma, entropy_term=args.entropy_term, use_baseline=True, device=device)
            
            cur_test_loss.append(test_loss.cpu().detach().numpy()) 
            
            for m, t, p in zip(test_mask.cpu().numpy().flatten(), test_y.cpu().numpy().flatten(), test_output.cpu().detach().numpy().flatten()):
                if np.equal(m, 1):
                    test_true.append(t)
                    test_pred.append(p)
        
        print('Test loss = %.4f'%(np.mean(np.array(cur_test_loss))))
        print('\n')
        test_pred = np.array(test_pred)
        test_pred = np.stack([1 - test_pred, test_pred], axis=1)
        test_ret = metrics.print_metrics_binary(test_true, test_pred)
=======
import numpy as np
import argparse
import os
import imp
import re
import pickle
import random
import matplotlib.pyplot as plt
import matplotlib as mpl

RANDOM_SEED = 12345
np.random.seed(RANDOM_SEED)
random.seed(RANDOM_SEED)

import torch
from torch import nn
import torch.nn.utils.rnn as rnn_utils
from torch.utils import data
from torch.autograd import Variable
import torch.nn.functional as F

torch.manual_seed(RANDOM_SEED)
torch.cuda.manual_seed(RANDOM_SEED)
torch.backends.cudnn.deterministic=True

from utils.decompensation import utils
from utils.decompensation.readers import DecompensationReader
from utils.decompensation.preprocessing import Discretizer, Normalizer
from utils.decompensation import metrics
from utils.decompensation import common_utils
from model import model_decomp

def parse_arguments(parser):
    parser.add_argument('--data_path', type=str, metavar='<data_path>', help='The path to the MIMIC-III data directory')
    parser.add_argument('--save_path', type=str, metavar='<data_path>', help='The path to save the model')
    parser.add_argument('--small_part', type=int, default=0, help='Use part of training data')
    parser.add_argument('--batch_size', type=int, default=128, help='Training batch size')
    parser.add_argument('--epochs', type=int, default=50, help='Training epochs')
    parser.add_argument('--lr', type=float, default=0.001, help='Learing rate')

    parser.add_argument('--cell', type=str, default='gru', help='RNN cell')
    parser.add_argument('--input_dim', type=int, default=76, help='Dimension of visit record data')
    parser.add_argument('--demo_dim', type=int, default=12, help='Dimension of demographic data')
    parser.add_argument('--rnn_dim', type=int, default=128, help='Dimension of hidden units in RNN')
    parser.add_argument('--agent_dim', type=int, default=128, help='Dimension of MLP in two agents')
    parser.add_argument('--mlp_dim', type=int, default=72, help='Dimension of MLP for output')
    parser.add_argument('--output_dim', type=int, default=1, help='Dimension of prediction target')
    parser.add_argument('--dropout_rate', type=float, default=0.5, help='Dropout rate')
    parser.add_argument('--lamda', type=float, default=0.5, help='Value of hyper-parameter lamda')
    parser.add_argument('--K', type=int, default=10, help='Value of hyper-parameter K')
    parser.add_argument('--gamma', type=float, default=0.9, help='Value of hyper-parameter gamma')
    parser.add_argument('--entropy_term', type=float, default=0.01, help='Value of reward entropy term')

    args = parser.parse_args()
    return args

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    args = parse_arguments(parser)

    ''' Prepare training data'''
    print('Preparing training data ... ')
    train_data_loader = common_utils.DeepSupervisionDataLoader(dataset_dir=os.path.join(
        args.data_path, 'train'), listfile=os.path.join(args.data_path, 'train_listfile.csv'), small_part=args.small_part)
    val_data_loader = common_utils.DeepSupervisionDataLoader(dataset_dir=os.path.join(
        args.data_path, 'train'), listfile=os.path.join(args.data_path, 'val_listfile.csv'), small_part=args.small_part)
    discretizer = Discretizer(timestep=1.0, store_masks=True,
                            impute_strategy='previous', start_time='zero')

    discretizer_header = discretizer.transform(train_data_loader._data["X"][0])[1].split(',')
    cont_channels = [i for (i, x) in enumerate(discretizer_header) if x.find("->") == -1]

    normalizer = Normalizer(fields=cont_channels)
    normalizer_state = 'decomp_normalizer'
    normalizer_state = os.path.join(os.path.dirname(args.data_path), normalizer_state)
    normalizer.load_params(normalizer_state)

    train_data_gen = utils.BatchGenDeepSupervision(train_data_loader, discretizer,
                                                    normalizer, args.batch_size, shuffle=True, return_names=True)
    val_data_gen = utils.BatchGenDeepSupervision(val_data_loader, discretizer,
                                                normalizer, args.batch_size, shuffle=False, return_names=True)

    demographic_data = []
    diagnosis_data = []
    idx_list = []

    demo_path = args.data_path + 'demographic/'
    for cur_name in os.listdir(demo_path):
        cur_id, cur_episode = cur_name.split('_', 1)
        cur_episode = cur_episode[:-4]
        cur_file = demo_path + cur_name

        with open(cur_file, "r") as tsfile:
            header = tsfile.readline().strip().split(',')
            if header[0] != "Icustay":
                continue
            cur_data = tsfile.readline().strip().split(',')
            
        if len(cur_data) == 1:
            cur_demo = np.zeros(12)
            cur_diag = np.zeros(128)
        else:
            if cur_data[3] == '':
                cur_data[3] = 60.0
            if cur_data[4] == '':
                cur_data[4] = 160
            if cur_data[5] == '':
                cur_data[5] = 60

            cur_demo = np.zeros(12)
            cur_demo[int(cur_data[1])] = 1
            cur_demo[5 + int(cur_data[2])] = 1
            cur_demo[9:] = cur_data[3:6]
            cur_diag = np.array(cur_data[8:], dtype=np.int)

        demographic_data.append(cur_demo)
        diagnosis_data.append(cur_diag)
        idx_list.append(cur_id+'_'+cur_episode)

    for each_idx in range(9,12):
        cur_val = []
        for i in range(len(demographic_data)):
            cur_val.append(demographic_data[i][each_idx])
        cur_val = np.array(cur_val)
        _mean = np.mean(cur_val)
        _std = np.std(cur_val)
        _std = _std if _std > 1e-7 else 1e-7
        for i in range(len(demographic_data)):
            demographic_data[i][each_idx] = (demographic_data[i][each_idx] - _mean) / _std


    '''Model structure'''
    print('Constructing model ... ')
    device = torch.device("cuda:0" if torch.cuda.is_available() == True else 'cpu')
    print("available device: {}".format(device))

    model = model_decomp.Agent(cell=args.cell,
                        use_baseline=True,
                        n_actions=args.K,
                        n_units=args.agent_dim,
                        n_input=args.input_dim,
                        demo_dim=args.demo_dim,
                        fusion_dim=args.mlp_dim,
                        n_hidden=args.rnn_dim,
                        n_output=args.output_dim,
                        dropout=args.dropout_rate,
                        lamda=args.lamda, device=device).to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=args.lr)

    '''Train phase'''
    print('Start training ... ')

    train_loss = []
    val_loss = []
    batch_loss = []
    max_auroc = 0

    file_name = args.save_path+'/agent_decomp'
    for each_chunk in range(args.epochs):
        cur_batch_loss = []
        #train_data_gen.steps
        model.train()
        for each_batch in range(train_data_gen.steps):
            batch_data = next(train_data_gen)
            batch_name = batch_data['names']
            batch_data = batch_data['data']
            
            batch_demo = []
            for i in range(len(batch_name)):
                cur_id, cur_ep, _ = batch_name[i].split('_', 2)
                cur_idx = cur_id + '_' + cur_ep
                cur_demo = torch.tensor(demographic_data[idx_list.index(cur_idx)], dtype=torch.float32)
                batch_demo.append(cur_demo)
            
            batch_demo = torch.stack(batch_demo).to(device)
            batch_x = torch.tensor(batch_data[0][0], dtype=torch.float32).to(device)
            batch_mask = torch.tensor(batch_data[0][1], dtype=torch.float32).unsqueeze(-1).to(device)
            batch_y = torch.tensor(batch_data[1], dtype=torch.float32).to(device)
            
            if batch_mask.size()[1] > 400:
                batch_x = batch_x[:, :400, :]
                batch_mask = batch_mask[:, :400, :]
                batch_y = batch_y[:, :400, :]
            optimizer.zero_grad()
            cur_output = model(batch_x, batch_demo) #B T 1
            loss, loss_rl, loss_task = model_decomp.get_loss(model, cur_output, batch_y, batch_mask, gamma=args.gamma, entropy_term=args.entropy_term, use_baseline=True, device=device)
            
            cur_batch_loss.append(loss.cpu().detach().numpy())

            loss.backward()
            optimizer.step()
            
            if each_batch % 500 == 0:
                print('Chunk %d, Batch %d: Loss = %.4f Task loss = %.4f RL loss = %.4f'%(each_chunk, each_batch, cur_batch_loss[-1], loss_task, loss_rl))

        batch_loss.append(cur_batch_loss)
        train_loss.append(np.mean(np.array(cur_batch_loss)))
        
        print("\n==>Predicting on validation")
        with torch.no_grad():
            model.eval()
            cur_val_loss = []
            cur_val_rl_loss = []
            cur_val_task_loss = []
            valid_true = []
            valid_pred = []
            for each_batch in range(val_data_gen.steps):
                valid_data = next(val_data_gen)
                valid_name = valid_data['names']
                valid_data = valid_data['data']
                
                valid_demo = []
                for i in range(len(valid_name)):
                    cur_id, cur_ep, _ = valid_name[i].split('_', 2)
                    cur_idx = cur_id + '_' + cur_ep
                    cur_demo = torch.tensor(demographic_data[idx_list.index(cur_idx)], dtype=torch.float32)
                    valid_demo.append(cur_demo)
                
                valid_demo = torch.stack(valid_demo).to(device)
                valid_x = torch.tensor(valid_data[0][0], dtype=torch.float32).to(device)
                valid_mask = torch.tensor(valid_data[0][1], dtype=torch.float32).unsqueeze(-1).to(device)
                valid_y = torch.tensor(valid_data[1], dtype=torch.float32).to(device)

                if valid_mask.size()[1] > 400:
                    valid_x = valid_x[:, :400, :]
                    valid_mask = valid_mask[:, :400, :]
                    valid_y = valid_y[:, :400, :]
                
                valid_output = model(valid_x, valid_demo)
                valid_loss, valid_loss_RL, valid_loss_task = model_decomp.get_loss(model, valid_output, valid_y, valid_mask, gamma=args.gamma, entropy_term=args.entropy_term, use_baseline=True, device=device)
                
                cur_val_loss.append(valid_loss.cpu().detach().numpy())
                cur_val_rl_loss.append(valid_loss_RL.cpu().detach().numpy())
                cur_val_task_loss.append(valid_loss_task.cpu().detach().numpy())

                for m, t, p in zip(valid_mask.cpu().numpy().flatten(), valid_y.cpu().numpy().flatten(), valid_output.cpu().detach().numpy().flatten()):
                    if np.equal(m, 1):
                        valid_true.append(t)
                        valid_pred.append(p)

            val_loss.append(np.mean(np.array(cur_val_loss)))
            print('Valid loss = %.4f Task loss = %.4f RL loss = %.4f'%(val_loss[-1], np.mean(np.array(cur_val_task_loss)), np.mean(np.array(cur_val_rl_loss))))
            print('\n')
            valid_pred = np.array(valid_pred)
            valid_pred = np.stack([1 - valid_pred, valid_pred], axis=1)
            ret = metrics.print_metrics_binary(valid_true, valid_pred)
            print()

            cur_auroc = ret['auroc']
            if cur_auroc > max_auroc:
                max_auroc = cur_auroc
                state = {
                    'net': model.state_dict(),
                    'optimizer': optimizer.state_dict(),
                    'chunk': each_chunk
                }
                torch.save(state, file_name)
                print('\n------------ Save best model ------------\n')


    '''Evaluate phase'''
    print('Testing model ... ')

    checkpoint = torch.load(file_name)
    save_chunk = checkpoint['chunk']
    print("last saved model is in chunk {}".format(save_chunk))
    model.load_state_dict(checkpoint['net'])
    optimizer.load_state_dict(checkpoint['optimizer'])
    model.eval()

    test_data_loader = common_utils.DeepSupervisionDataLoader(dataset_dir=os.path.join(args.data_path, 'test'),
                                                                    listfile=os.path.join(args.data_path, 'test_listfile.csv'), small_part=args.small_part)
    test_data_gen = utils.BatchGenDeepSupervision(test_data_loader, discretizer,
                                                normalizer, args.batch_size,
                                                shuffle=False, return_names=True)

    with torch.no_grad():
        torch.manual_seed(RANDOM_SEED)
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(RANDOM_SEED)
    
        cur_test_loss = []
        test_true = []
        test_pred = []
        
        for each_batch in range(test_data_gen.steps):
            test_data = next(test_data_gen)
            test_name = test_data['names']
            test_data = test_data['data']

            test_demo = []
            for i in range(len(test_name)):
                cur_id, cur_ep, _ = test_name[i].split('_', 2)
                cur_idx = cur_id + '_' + cur_ep
                cur_demo = torch.tensor(demographic_data[idx_list.index(cur_idx)], dtype=torch.float32)
                test_demo.append(cur_demo)

            test_demo = torch.stack(test_demo).to(device)
            test_x = torch.tensor(test_data[0][0], dtype=torch.float32).to(device)
            test_mask = torch.tensor(test_data[0][1], dtype=torch.float32).unsqueeze(-1).to(device)
            test_y = torch.tensor(test_data[1], dtype=torch.float32).to(device)
            
            if test_mask.size()[1] > 400:
                test_x = test_x[:, :400, :]
                test_mask = test_mask[:, :400, :]
                test_y = test_y[:, :400, :]
            
            test_output = model(test_x, test_demo)
            test_loss, _, _ = model_decomp.get_loss(model, test_output, test_y, test_mask, gamma=args.gamma, entropy_term=args.entropy_term, use_baseline=True, device=device)
            
            cur_test_loss.append(test_loss.cpu().detach().numpy()) 
            
            for m, t, p in zip(test_mask.cpu().numpy().flatten(), test_y.cpu().numpy().flatten(), test_output.cpu().detach().numpy().flatten()):
                if np.equal(m, 1):
                    test_true.append(t)
                    test_pred.append(p)
        
        print('Test loss = %.4f'%(np.mean(np.array(cur_test_loss))))
        print('\n')
        test_pred = np.array(test_pred)
        test_pred = np.stack([1 - test_pred, test_pred], axis=1)
        test_ret = metrics.print_metrics_binary(test_true, test_pred)
>>>>>>> 742f7a833bbc02da15cd7752dc1d2cdd8606aeb1
