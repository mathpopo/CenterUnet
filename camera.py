from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import sys
import json
import argparse
import cv2
import numpy as np
import time
import torch

from external.nms import soft_nms

from utils.logger import Logger
from config import Config
from dataset.coco import COCO
from models.network import create_model, load_model, save_model
from detector import CtdetDetector as Detector
from utils.debugger import colors
from utils.image import size2level, levelnum

COCO_NAMES = ['__background__', 'person', 'bicycle', 'car', 'motorcycle', 'airplane',
              'bus', 'train', 'truck', 'boat', 'traffic light', 'fire hydrant',
              'stop sign', 'parking meter', 'bench', 'bird', 'cat', 'dog', 'horse',
              'sheep', 'cow', 'elephant', 'bear', 'zebra', 'giraffe', 'backpack',
              'umbrella', 'handbag', 'tie', 'suitcase', 'frisbee', 'skis',
              'snowboard', 'sports ball', 'kite', 'baseball bat', 'baseball glove',
              'skateboard', 'surfboard', 'tennis racket', 'bottle', 'wine glass',
              'cup', 'fork', 'knife', 'spoon', 'bowl', 'banana', 'apple', 'sandwich',
              'orange', 'broccoli', 'carrot', 'hot dog', 'pizza', 'donut', 'cake',
              'chair', 'couch', 'potted plant', 'bed', 'dining table', 'toilet', 'tv',
              'laptop', 'mouse', 'remote', 'keyboard', 'cell phone', 'microwave',
              'oven', 'toaster', 'sink', 'refrigerator', 'book', 'clock', 'vase',
              'scissors', 'teddy bear', 'hair drier', 'toothbrush']

def get_args():
    # Training settings
    parser = argparse.ArgumentParser(description='Object Detection!')
    parser.add_argument('--finetuning', action='store_true', default=False, help='finetuning the training')
    parser.add_argument('--without_gpu', action='store_true', default=True, help='no use gpu')

    parser.add_argument('--num_workers', type=int, default=4, help='dataloader threads. 0 for single-thread.')
    parser.add_argument('--batch_size', type=int, default=5, help='batch size')
    parser.add_argument('--num_epochs', type=int, default=140, help='total training epochs.')
    parser.add_argument('--save_all', action='store_true', help='save model to disk every 5 epochs.')
    parser.add_argument('--num_iters', type=int, default=-1, help='default: #samples / batch_size.')
    parser.add_argument('--val_intervals', type=int, default=5, help='number of epochs to run validation.')
    parser.add_argument('--trainval', action='store_true', help='include validation in training and test on test set')

    parser.add_argument('--lr', type=float, default=1.25e-4, help='learning rate for batch size 32.')
    parser.add_argument('--lr_step', type=str, default='90,120', help='drop learning rate by 10.')

    parser.add_argument('--sizeaug', action='store_true', default=False, help='size augmentation')

    parser.add_argument('--gpus', default='0', help='-1 for CPU, use comma for multiple gpus')
    parser.add_argument('--seed', type=int, default=326, help='random seed')

    parser.add_argument('--load_model', default='./save_models/model_last.pth', help='path to pretrained model')
    parser.add_argument('--resume', action='store_true', help='resume training')

    parser.add_argument('--test', action='store_true')

    parser.add_argument('--metric', default='loss', help='main metric to save best model')

    parser.add_argument('--image', default='./test.jpg', help='test image')
    parser.add_argument('--nms', action='store_true', default=False, help='nms')

    parser.add_argument('--network_type', type=str, default='unetobj', help='network type')
    parser.add_argument('--backbone', type=str, default='peleenet', help='backbone network')

    parser.add_argument('--output_dir', default='./results', help='output dir')
    parser.add_argument('--center_thresh', type=float, default=0.10, help='center threshold')
    parser.add_argument('--sizethr', type=float, default=0.03, help='size threshold')
    parser.add_argument('--instance', type=int, default=0, help='instance number')

    parser.add_argument('--imsz', type=int, default=512, help='image size')

    args = parser.parse_args()
    print(args)
    return args

def cleanmask(m0, m1, m2, m3):
    return (m0 * ((m0-m1)>0.))

def assignroi(pagenum, dst, src, x1, y1, x2, y2):
  dst[y1:y2, x1:x2] += src[y1:y2, x1:x2, pagenum]

def test():
  args = get_args()

  args.gpus_str = args.gpus
  args.gpus = [int(gpu) for gpu in args.gpus.split(',')]
  args.gpus = [i for i in range(len(args.gpus))] if args.gpus[0] >=0 else [-1]

  if not args.without_gpu:
      print("Use GPU")
      os.environ['CUDA_VISIBLE_DEVICES'] = args.gpus_str
      device = torch.device('cuda')
  else:
      print("Use CPU")
      device = torch.device('cpu')
      args.gpus = []

  if args.network_type == 'large_hourglass':
      down_ratio = 4
      nstack = 2 
  else:
      down_ratio = 2 if args.backbone == 'peleenet' else 1
      nstack = 1

  cfg = Config(
          args.gpus, device,
          args.network_type, args.backbone,
          1, down_ratio, nstack
          )

  cfg.input_h = cfg.input_w = cfg.input_res = args.imsz

  cfg.load_model = args.load_model
  cfg.nms = args.nms
  cfg.debug = 0
  cfg.center_thresh = args.center_thresh

  logger = Logger(cfg)

  cfg.update(COCO)
  assert(cfg.num_maskclasses == 9)

  detector = Detector(cfg, args.output_dir)

  cap = cv2.VideoCapture(0)

  while True:
    ret, img = cap.read()

    h, w, _ = img.shape
    scale = 512/w
    w = 512
    h = int(h*scale)
    img = cv2.resize(img, (w,h)) 

    ret = detector.run(img)

    bbox_and_scores = ret['results']

    inp_height = ret['meta']['inp_height']
    inp_width = ret['meta']['inp_width']
    new_height = ret['meta']['new_height']
    new_width = ret['meta']['new_width']
    trans_inv = ret['meta']['trans_inv']

    heatmap = ret['heatmap']
    heatmap = cv2.resize(heatmap, (inp_width,inp_height))
    heatmap = cv2.warpAffine(heatmap, trans_inv, (new_width, new_height), flags=cv2.INTER_LINEAR)

    allmasktmp = ret['allmask']
    allmask = np.zeros((h,w,allmasktmp.shape[2]), dtype=np.float32)

    for i in range(0, allmask.shape[2]):
      allmaskpg = allmasktmp[:,:,i]

      allmaskpg = cv2.resize(allmaskpg, (inp_width,inp_height))
      allmaskpg = cv2.warpAffine(allmaskpg, trans_inv, (new_width, new_height), flags=cv2.INTER_LINEAR)
      allmask[:,:,i] = cv2.resize(allmaskpg, (w,h))

    allmaskjpg = np.zeros((h,w,3), dtype=np.uint8)

    i = 0
    thr = 0.01

    for key in bbox_and_scores:
      for box in bbox_and_scores[key]:
        if box[4] > cfg.center_thresh:
          x1 = int(box[0])
          y1 = int(box[1])
          x2 = int(box[2])
          y2 = int(box[3])

          if x2 <= x1 or (x1 < 0 and x2 < 0):
              continue

          if y2 <= y1 or (y1 < 0 and y2 < 0):
              continue

          x1 = 0 if x1 < 0 else x1
          y1 = 0 if y1 < 0 else y1
          x2 = w - 1 if x2 >= w else x2
          y2 = h - 1 if y2 >= h else y2

          # deal with mask begin
          cls = key - 1

          centerx = (x1+x2)//2
          centery = (y1+y2)//2

          # clsbase = cls*9
          clsbase = 0

          allmaskroi = allmask[y1:y2, x1:x2, :]
          roi_h, roi_w, _ = allmaskroi.shape

          if roi_h < 6 or roi_w < 6:
            continue

          roi_cx = roi_w//2
          roi_cy = roi_h//2
          cell_w = (roi_w+5)//6
          cell_h = (roi_h+5)//6

          roi = np.zeros((roi_h,roi_w), dtype=np.float32)

          # TOP
          assignroi(0, roi, allmaskroi, 0,             0,             roi_cx-cell_w, roi_cy-cell_h)
          assignroi(1, roi, allmaskroi, roi_cx-cell_w, 0,             roi_cx+cell_w, roi_cy-cell_h)
          assignroi(2, roi, allmaskroi, roi_cx+cell_w, 0,             roi_w,         roi_cy-cell_h)

          # MIDDLE
          assignroi(3, roi, allmaskroi, 0,             roi_cy-cell_h, roi_cx-cell_w, roi_cy+cell_h)
          assignroi(4, roi, allmaskroi, roi_cx-cell_w, roi_cy-cell_h, roi_cx+cell_w, roi_cy+cell_h)
          assignroi(5, roi, allmaskroi, roi_cx+cell_w, roi_cy-cell_h, roi_w,         roi_cy+cell_h)

          # BOTTOM
          assignroi(6, roi, allmaskroi, 0,             roi_cy+cell_h, roi_cx-cell_w, roi_h        )
          assignroi(7, roi, allmaskroi, roi_cx-cell_w, roi_cy+cell_h, roi_cx+cell_w, roi_h        )
          assignroi(8, roi, allmaskroi, roi_cx+cell_w, roi_cy+cell_h, roi_w,         roi_h        )

          # roi = np.amax(allmaskroi[:,:,:], axis=2)
          roi = (roi > thr).astype(np.uint8)

          rgb = colors[i,0,0].tolist()
          i += 1

          if args.instance != 0 and args.instance != i:
              continue

          l = size2level(w*h, roi_w*roi_h)
          roi = roi*((allmaskroi[:,:,cfg.num_maskclasses+l]+allmaskroi[:,:,cfg.num_maskclasses+l+1])/2.0>args.sizethr)

          allmaskjpg[:,:,0][y1:y2,x1:x2] += roi*rgb[0]
          allmaskjpg[:,:,1][y1:y2,x1:x2] += roi*rgb[1]
          allmaskjpg[:,:,2][y1:y2,x1:x2] += roi*rgb[2]
          # deal with mask begin

          cat = COCO._valid_ids[key-1]
          cat = COCO.all_valid_ids.index(cat)+1

          print(x1, y1, x2, y2, COCO_NAMES[cat], box[4], "level", l)

          cv2.rectangle(img, (x1, y1), (x2, y2), (0, 255, 0), 2)
          cv2.putText(img, COCO_NAMES[cat], (x1, y1), cv2.FONT_HERSHEY_SIMPLEX, 1, (cat*2, 255, 255-cat), 2)

    cv2.imshow("centerunet1", img)
    cv2.imshow("centerunet2", allmaskjpg)

    if cv2.waitKey(1) & 0xFF == ord('q'):
        break

if __name__ == '__main__':
  test()
