#  Copyright (c) 5.2021. Yinyu Nie
#  License: MIT

import torch.utils.data
from torch.utils.data import DataLoader
from torch.utils.data.distributed import DistributedSampler
import numpy as np
from models.datasets import Base_Dataset
import h5py
import os
from utils.pc_utils import rot2head
import random

default_collate = torch.utils.data.dataloader.default_collate


class MLP_Dataset(Base_Dataset):
    def __init__(self,cfg, mode):
        super(MLP_Dataset, self).__init__(cfg, mode)
        self.aug = mode == 'train'
        self.num_frames = cfg.config['data']['num_frames']
        self.use_height = not cfg.config['data']['no_height']
        self.max_num_obj = cfg.config['data']['max_gt_boxes']
        if self.aug:
            self.flip_matrix = np.array([[0, 0, 1], [0, 1, 0], [1, 0, 0]])
            self.rot_func = lambda theta: np.array([[np.cos(theta), 0., -np.sin(theta)],
                                                    [0., 1., 0.],
                                                    [np.sin(theta), 0, np.cos(theta)]])
            self.offset_func = lambda scale: np.array([1., 0., 1.]) * scale

    def augment_data(self, skeleton_joints, object_nodes):
        '''Augment training data'''
        if_flip = random.randint(0, 1)
        rot_angle = np.random.choice([-np.pi, -0.5 * np.pi, 0, 0.5 * np.pi])
        offset_scale = random.uniform(-1., 1.)
        rot_mat = self.rot_func(rot_angle)
        offset = self.offset_func(offset_scale)

        '''begin to augment'''
        if if_flip:
            '''begin to flip'''
            # flip skeleton
            skeleton_joints = np.dot(skeleton_joints, self.flip_matrix)
            # flip object bboxes
            for node in object_nodes:
                node['centroid'] = np.dot(np.array(node['centroid']), self.flip_matrix)
                R_mat = np.array(node['R_mat']).dot(self.flip_matrix)
                R_mat[2] = np.cross(R_mat[0], R_mat[1])
                node['R_mat'] = R_mat

        '''begin to rotate'''
        # rotate skeleton
        skeleton_joints = np.dot(skeleton_joints, rot_mat)
        # rotate object bboxes
        for node in object_nodes:
            node['centroid'] = np.dot(np.array(node['centroid']), rot_mat)
            node['R_mat'] = np.array(node['R_mat']).dot(rot_mat)

        '''begin to translate'''
        # translate skeleton
        skeleton_joints += offset
        # translate object nodes
        for node in object_nodes:
            node['centroid'] += offset

        return skeleton_joints, object_nodes

    def __getitem__(self, idx):
        '''Get each sample'''
        '''Load data'''
        data_path = self.split[idx]
        use_memory = True
        instances = []

        if use_memory == True:
            sample_data = self.id_file_dict[data_path]
            skeleton_joints = sample_data['skeleton_joints']
            object_nodes = sample_data['object_nodes']
            shape_codes = sample_data['shape_codes']
            seed_features = sample_data['nearest_seed_skeleton_features']
            # print(object_nodes.keys())
            # print(type(skeleton_joints))
                        
            for instance_id in object_nodes.keys():
                object_node = object_nodes[instance_id]
                instance = {'centroid': object_node['centroid'][:],
                            'R_mat': object_node['R_mat'][:], 
                            'size': object_node['size'][:]}
                instances.append(instance)

        else:
            sample_data = h5py.File(data_path, "r")
            skeleton_joints = sample_data['skeleton_joints'][:]
            object_nodes = sample_data['object_nodes']
            shape_codes = sample_data['shape_codes'][:]
        
            for instance_id in object_nodes.keys():
                object_node = object_nodes[instance_id]
                instance = {'centroid': object_node['centroid'][:],
                            'R_mat': object_node['R_mat'][:], 
                            'size': object_node['size'][:]}
                instances.append(instance)

            sample_data.close()

        '''Augment data'''
        if self.aug:
            skeleton_joints, instances = self.augment_data(skeleton_joints, instances)
        boxes3D = []
        for instance in instances:
            heading = rot2head(instance['R_mat'])
            box3D = np.hstack([instance['centroid'], np.log(instance['size']), np.sin(heading), np.cos(heading)])
            boxes3D.append(box3D)

        boxes3D = np.array(boxes3D)

        if self.use_height:
            floor_height = np.percentile(skeleton_joints[..., 1], 0.99)
            height = skeleton_joints[..., 1] - floor_height
            skeleton_joints = np.concatenate([skeleton_joints, np.expand_dims(height, -1)], -1)


        # Process input frames
        joint_ids = np.linspace(0, skeleton_joints.shape[0]-1, self.num_frames).round().astype(np.uint16)
        input_joints = skeleton_joints[joint_ids]

        # deliver to network
        ret_dict = {}
        ret_dict['input_joints'] = input_joints.astype(np.float32)
        ret_dict['shape_codes'] = shape_codes.astype(np.float32)
        
        #adl input
        #stack size
        bb_count = len(boxes3D)
        padded_boxes3D = np.pad(boxes3D, [(0, 10-bb_count), (0, 0)], mode='constant').astype(np.float32)
        #ret_dict['adl_input'] = np.hstack([padded_boxes3D.flatten(), ret_dict['input_joints'].flatten()])
        ret_dict['adl_input'] = {
            'bounding_boxes': padded_boxes3D,
            'seed_features': seed_features
        }
        
        ret_dict['adl_output'] = ret_dict['shape_codes']
        
        return ret_dict

def collate_fn(batch):
    '''
    data collater
    :param batch:
    :return:
    '''
    collated_batch = {}
    for key in batch[0]:
        if key not in ['sample_idx']:
            collated_batch[key] = default_collate([elem[key] for elem in batch])
        else:
            collated_batch[key] = [elem[key] for elem in batch]
    return collated_batch

class Custom_Dataloader(object):
    def __init__(self, dataloader, sampler):
        self.dataloader = dataloader
        self.sampler = sampler

# Init datasets and dataloaders
def my_worker_init_fn(worker_id):
    np.random.seed(np.random.get_state()[1][0] + worker_id)

# Init datasets and dataloaders
def MLP_dataloader(cfg, mode='train'):
    if cfg.config['data']['dataset'] == 'virtualhome':
        dataset = MLP_Dataset(cfg, mode)
    else:
        raise NotImplementedError

    if cfg.config['device']['distributed']:
        sampler = DistributedSampler(dataset, shuffle=(mode == 'train'))
    else:
        if mode=='train':
            sampler = torch.utils.data.RandomSampler(dataset)
        else:
            sampler = torch.utils.data.SequentialSampler(dataset)

    batch_sampler = torch.utils.data.BatchSampler(sampler, batch_size=cfg.config[mode]['batch_size'],
                                                  drop_last=False)

    dataloader = DataLoader(dataset=dataset,
                            batch_sampler=batch_sampler,
                            num_workers=cfg.config['device']['num_workers'],
                            collate_fn=collate_fn,
                            worker_init_fn=my_worker_init_fn)

    dataloader = Custom_Dataloader(dataloader, sampler)
    return dataloader
