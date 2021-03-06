# ------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.
# ------------------------------------------------------------------------------

from collections import deque

import cv2
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from models import *
from models.decode import mot_decode
from models.model import create_model, load_model
from models.utils import _tranpose_and_gather_feat, _tranpose_and_gather_feat_expand
from tracker import matching
from tracking_utils.kalman_filter import KalmanFilter
from tracking_utils.log import logger
from tracking_utils.utils import *
from utils.post_process import ctdet_post_process

from cython_bbox import bbox_overlaps as bbox_ious

from .basetrack import BaseTrack, TrackState

from scipy.optimize import linear_sum_assignment
import random
import pickle
import copy


class GaussianBlurConv(nn.Module):
    def __init__(self, channels=3):
        super(GaussianBlurConv, self).__init__()
        self.channels = channels
        kernel = [[0.00078633, 0.00655965, 0.01330373, 0.00655965, 0.00078633],
                  [0.00655965, 0.05472157, 0.11098164, 0.05472157, 0.00655965],
                  [0.01330373, 0.11098164, 0.22508352, 0.11098164, 0.01330373],
                  [0.00655965, 0.05472157, 0.11098164, 0.05472157, 0.00655965],
                  [0.00078633, 0.00655965, 0.01330373, 0.00655965, 0.00078633]]
        kernel = torch.FloatTensor(kernel).unsqueeze(0).unsqueeze(0)
        kernel = np.repeat(kernel, self.channels, axis=0)
        self.weight = nn.Parameter(data=kernel, requires_grad=False)

    def __call__(self, x):
        x = F.conv2d(x, self.weight, padding=2, groups=self.channels)
        return x


gaussianBlurConv = GaussianBlurConv().cuda()

seed = 0
random.seed(seed)
np.random.seed(seed)
if torch.cuda.is_available():
    torch.cuda.manual_seed_all(seed)

torch.manual_seed(seed)
torch.cuda.manual_seed(seed)
torch.cuda.manual_seed_all(seed)

# Remove randomness (may be slower on Tesla GPUs)
# https://pytorch.org/docs/stable/notes/randomness.html
if seed == 0:
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False
smoothL1 = torch.nn.SmoothL1Loss()
mse = torch.nn.MSELoss()

td_ = {}


def bbox_dis(bbox1, bbox2):
    center1 = (bbox1[:, :2] + bbox1[:, 2:]) / 2
    center2 = (bbox2[:, :2] + bbox2[:, 2:]) / 2
    center1 = np.repeat(center1.reshape(-1, 1, 2), len(bbox2), axis=1)
    center2 = np.repeat(center2.reshape(1, -1, 2), len(bbox1), axis=0)
    dis = np.sqrt(np.sum((center1 - center2) ** 2, axis=-1))
    return dis

class STrack(BaseTrack):
    shared_kalman = KalmanFilter()
    shared_kalman_ = KalmanFilter()

    def __init__(self, tlwh, score, temp_feat, buffer_size=30):

        # wait activate
        self._tlwh = np.asarray(tlwh, dtype=np.float)
        self.kalman_filter = None
        self.mean, self.covariance = None, None
        self.is_activated = False

        self.score = score
        self.tracklet_len = 0
        self.exist_len = 1

        self.smooth_feat = None
        self.smooth_feat_ad = None

        self.update_features(temp_feat)
        self.features = deque([], maxlen=buffer_size)
        self.alpha = 0.9

        self.curr_tlbr = self.tlwh_to_tlbr(self._tlwh)

        self.det_dict = {}

    def get_v(self):
        return self.mean[4:6] if self.mean is not None else None

    def update_features_ad(self, feat):
        feat /= np.linalg.norm(feat)
        if self.smooth_feat_ad is None:
            self.smooth_feat_ad = feat
        else:
            self.smooth_feat_ad = self.alpha * self.smooth_feat_ad + (1 - self.alpha) * feat
        self.smooth_feat_ad /= np.linalg.norm(self.smooth_feat_ad)

    def update_features(self, feat):
        feat /= np.linalg.norm(feat)
        self.curr_feat = feat
        if self.smooth_feat is None:
            self.smooth_feat = feat
        else:
            self.smooth_feat = self.alpha * self.smooth_feat + (1 - self.alpha) * feat
        self.features.append(feat)
        self.smooth_feat /= np.linalg.norm(self.smooth_feat)

    def predict(self):
        mean_state = self.mean.copy()
        if self.state != TrackState.Tracked:
            mean_state[7] = 0
        self.mean, self.covariance = self.kalman_filter.predict(mean_state, self.covariance)

    @staticmethod
    def multi_predict(stracks):
        if len(stracks) > 0:
            multi_mean = np.asarray([st.mean.copy() for st in stracks])
            multi_covariance = np.asarray([st.covariance for st in stracks])
            for i, st in enumerate(stracks):
                if st.state != TrackState.Tracked:
                    multi_mean[i][7] = 0
            multi_mean, multi_covariance = STrack.shared_kalman.multi_predict(multi_mean, multi_covariance)
            for i, (mean, cov) in enumerate(zip(multi_mean, multi_covariance)):
                stracks[i].mean = mean
                stracks[i].covariance = cov

    @staticmethod
    def multi_predict_(stracks):
        if len(stracks) > 0:
            multi_mean = np.asarray([st.mean.copy() for st in stracks])
            multi_covariance = np.asarray([st.covariance for st in stracks])
            for i, st in enumerate(stracks):
                if st.state != TrackState.Tracked:
                    multi_mean[i][7] = 0
            multi_mean, multi_covariance = STrack.shared_kalman_.multi_predict(multi_mean, multi_covariance)
            for i, (mean, cov) in enumerate(zip(multi_mean, multi_covariance)):
                stracks[i].mean = mean
                stracks[i].covariance = cov

    def activate(self, kalman_filter, frame_id, track_id=None):
        """Start a new tracklet"""
        self.kalman_filter = kalman_filter
        if track_id:
            self.track_id = track_id['track_id']
            track_id['track_id'] += 1
        else:
            self.track_id = self.next_id()
        self.mean, self.covariance = self.kalman_filter.initiate(self.tlwh_to_xyah(self._tlwh))

        self.tracklet_len = 0
        self.state = TrackState.Tracked
        self.is_activated = True
        self.frame_id = frame_id
        self.start_frame = frame_id

    def activate_(self, kalman_filter, frame_id, track_id=None):
        """Start a new tracklet"""
        self.kalman_filter = kalman_filter
        if track_id:
            self.track_id = track_id['track_id']
            track_id['track_id'] += 1
        else:
            self.track_id = self.next_id_()
        self.mean, self.covariance = self.kalman_filter.initiate(self.tlwh_to_xyah(self._tlwh))

        self.tracklet_len = 0
        self.state = TrackState.Tracked
        self.is_activated = True
        self.frame_id = frame_id
        self.start_frame = frame_id

    def re_activate(self, new_track, frame_id, new_id=False):
        self.curr_tlbr = self.tlwh_to_tlbr(new_track.tlwh)
        self.mean, self.covariance = self.kalman_filter.update(
            self.mean, self.covariance, self.tlwh_to_xyah(new_track.tlwh)
        )

        self.update_features(new_track.curr_feat)
        self.tracklet_len = 0
        self.exist_len += 1
        self.state = TrackState.Tracked
        self.is_activated = True
        self.frame_id = frame_id
        if new_id:
            self.track_id = self.next_id()

    def re_activate_(self, new_track, frame_id, new_id=False):
        self.curr_tlbr = self.tlwh_to_tlbr(new_track.tlwh)
        self.mean, self.covariance = self.kalman_filter.update(
            self.mean, self.covariance, self.tlwh_to_xyah(new_track.tlwh)
        )

        self.update_features(new_track.curr_feat)
        self.tracklet_len = 0
        self.exist_len += 1
        self.state = TrackState.Tracked
        self.is_activated = True
        self.frame_id = frame_id
        if new_id:
            self.track_id = self.next_id_()

    def update(self, new_track, frame_id, update_feature=True):
        """
        Update a matched track
        :type new_track: STrack
        :type frame_id: int
        :type update_feature: bool
        :return:
        """
        self.frame_id = frame_id
        self.tracklet_len += 1
        self.exist_len += 1

        self.curr_tlbr = self.tlwh_to_tlbr(new_track.tlwh)
        new_tlwh = new_track.tlwh
        self.mean, self.covariance = self.kalman_filter.update(
            self.mean, self.covariance, self.tlwh_to_xyah(new_tlwh))
        self.state = TrackState.Tracked
        self.is_activated = True

        self.score = new_track.score
        if update_feature:
            self.update_features(new_track.curr_feat)

    @property
    # @jit(nopython=True)
    def tlwh(self):
        """Get current position in bounding box format `(top left x, top left y,
                width, height)`.
        """
        if self.mean is None:
            return self._tlwh.copy()
        ret = self.mean[:4].copy()
        ret[2] *= ret[3]
        ret[:2] -= ret[2:] / 2
        return ret

    @property
    # @jit(nopython=True)
    def tlbr(self):
        """Convert bounding box to format `(min x, min y, max x, max y)`, i.e.,
        `(top left, bottom right)`.
        """
        ret = self.tlwh.copy()
        ret[2:] += ret[:2]
        return ret

    @staticmethod
    # @jit(nopython=True)
    def tlwh_to_xyah(tlwh):
        """Convert bounding box to format `(center x, center y, aspect ratio,
        height)`, where the aspect ratio is `width / height`.
        """
        ret = np.asarray(tlwh).copy()
        ret[:2] += ret[2:] / 2
        ret[2] /= ret[3]
        return ret

    def to_xyah(self):
        return self.tlwh_to_xyah(self.tlwh)

    @staticmethod
    # @jit(nopython=True)
    def tlbr_to_tlwh(tlbr):
        ret = np.asarray(tlbr).copy()
        ret[2:] -= ret[:2]
        return ret

    @staticmethod
    # @jit(nopython=True)
    def tlwh_to_tlbr(tlwh):
        ret = np.asarray(tlwh).copy()
        ret[2:] += ret[:2]
        return ret

    def __repr__(self):
        return 'OT_{}_({}-{})'.format(self.track_id, self.start_frame, self.end_frame)


class JDETracker(object):
    def __init__(
            self,
            opt,
            frame_rate=30,
            tracked_stracks=[],
            lost_stracks=[],
            removed_stracks=[],
            frame_id=0,
            ad_last_info={},
            model=None
    ):
        self.opt = opt
        print('Creating model...')
        if model:
            self.model = model
        else:
            self.model = create_model(opt.arch, opt.heads, opt.head_conv)
            self.model = load_model(self.model, opt.load_model).cuda()
        self.model.eval()

        self.log_index = []
        self.unconfirmed_ad_iou = None
        self.tracked_stracks_ad_iou = None
        self.strack_pool_ad_iou = None

        self.tracked_stracks = copy.deepcopy(tracked_stracks)  # type: list[STrack]
        self.lost_stracks = copy.deepcopy(lost_stracks)  # type: list[STrack]
        self.removed_stracks = copy.deepcopy(removed_stracks)  # type: list[STrack]

        self.tracked_stracks_ad = copy.deepcopy(tracked_stracks)  # type: list[STrack]
        self.lost_stracks_ad = copy.deepcopy(lost_stracks)  # type: list[STrack]
        self.removed_stracks_ad = copy.deepcopy(removed_stracks)  # type: list[STrack]

        self.tracked_stracks_ = copy.deepcopy(tracked_stracks)  # type: list[STrack]
        self.lost_stracks_ = copy.deepcopy(lost_stracks)  # type: list[STrack]
        self.removed_stracks_ = copy.deepcopy(removed_stracks)  # type: list[STrack]

        self.frame_id = frame_id
        self.frame_id_ = frame_id
        self.frame_id_ad = frame_id

        self.det_thresh = opt.conf_thres
        self.buffer_size = int(frame_rate / 30.0 * opt.track_buffer)
        self.max_time_lost = self.buffer_size
        self.max_per_image = 128

        self.kalman_filter = KalmanFilter()
        self.kalman_filter_ad = KalmanFilter()
        self.kalman_filter_ = KalmanFilter()

        self.attacked_ids = set([])
        self.low_iou_ids = set([])
        self.ATTACK_IOU_THR = opt.iou_thr
        self.attack_iou_thr = self.ATTACK_IOU_THR
        self.ad_last_info = copy.deepcopy(ad_last_info)
        self.FRAME_THR = 10

        self.temp_i = 0
        self.multiple_ori_ids = {}
        self.multiple_att_ids = {}
        self.multiple_ori2att = {}
        self.multiple_att_freq = {}

        # hijacking attack
        self.ad_bbox = True
        self.ad_ids = set([])

    def post_process(self, dets, meta):
        dets = dets.detach().cpu().numpy()
        dets = dets.reshape(1, -1, dets.shape[2])
        dets = ctdet_post_process(
            dets.copy(), [meta['c']], [meta['s']],
            meta['out_height'], meta['out_width'], self.opt.num_classes)
        for j in range(1, self.opt.num_classes + 1):
            dets[0][j] = np.array(dets[0][j], dtype=np.float32).reshape(-1, 5)
        return dets[0]

    def merge_outputs(self, detections):
        results = {}
        for j in range(1, self.opt.num_classes + 1):
            results[j] = np.concatenate(
                [detection[j] for detection in detections], axis=0).astype(np.float32)

        scores = np.hstack(
            [results[j][:, 4] for j in range(1, self.opt.num_classes + 1)])
        if len(scores) > self.max_per_image:
            kth = len(scores) - self.max_per_image
            thresh = np.partition(scores, kth)[kth]
            for j in range(1, self.opt.num_classes + 1):
                keep_inds = (results[j][:, 4] >= thresh)
                results[j] = results[j][keep_inds]
        return results

    @staticmethod
    def recoverImg(im_blob, img0):
        height = 608
        width = 1088
        im_blob = im_blob.cpu() * 255.0
        shape = img0.shape[:2]  # shape = [height, width]
        ratio = min(float(height) / shape[0], float(width) / shape[1])
        new_shape = (round(shape[1] * ratio), round(shape[0] * ratio))  # new_shape = [width, height]
        dw = (width - new_shape[0]) / 2  # width padding
        dh = (height - new_shape[1]) / 2  # height padding
        top, bottom = round(dh - 0.1), round(dh + 0.1)
        left, right = round(dw - 0.1), round(dw + 0.1)

        im_blob = im_blob.squeeze().permute(1, 2, 0)[top:height - bottom, left:width - right, :].numpy().astype(
            np.uint8)
        im_blob = cv2.cvtColor(im_blob, cv2.COLOR_RGB2BGR)

        h, w, _ = img0.shape
        im_blob = cv2.resize(im_blob, (w, h))

        return im_blob

    def recoverNoise(self, noise, img0):
        height = 608
        width = 1088
        shape = img0.shape[:2]  # shape = [height, width]
        ratio = min(float(height) / shape[0], float(width) / shape[1])
        new_shape = (round(shape[1] * ratio), round(shape[0] * ratio))  # new_shape = [width, height]
        dw = (width - new_shape[0]) / 2  # width padding
        dh = (height - new_shape[1]) / 2  # height padding
        top, bottom = round(dh - 0.1), round(dh + 0.1)
        left, right = round(dw - 0.1), round(dw + 0.1)

        noise = noise[:, :, top:height - bottom, left:width - right]
        h, w, _ = img0.shape
        # noise = self.resizeTensor(noise, h, w).cpu().squeeze().permute(1, 2, 0).numpy()
        noise = noise.cpu().squeeze().permute(1, 2, 0).numpy()

        noise = (noise[:, :, ::-1] * 255).astype(np.int)

        return noise

    @staticmethod
    def resizeTensor(tensor, height, width):
        h = torch.linspace(-1, 1, height).view(-1, 1).repeat(1, width).to(tensor.device)
        w = torch.linspace(-1, 1, width).repeat(height, 1).to(tensor.device)
        grid = torch.cat((h.unsqueeze(2), w.unsqueeze(2)), dim=2)
        grid = grid.unsqueeze(0)

        output = F.grid_sample(tensor, grid=grid, mode='bilinear', align_corners=True)
        return output

    @staticmethod
    def processIoUs(ious):
        h, w = ious.shape
        assert h == w
        ious = np.tril(ious, -1)
        index = np.argsort(-ious.reshape(-1))
        indSet = set([])
        for ind in index:
            i = ind // h
            j = ind % w
            if ious[i, j] == 0:
                break
            if i in indSet or j in indSet:
                ious[i, j] = 0
            else:
                indSet.add(i)
                indSet.add(j)
        return ious

    def attack_sg_hj(
            self,
            im_blob,
            img0,
            dets,
            inds,
            remain_inds,
            last_info,
            outputs_ori,
            attack_id,
            attack_ind,
            ad_bbox,
            track_v
    ):
        noise = torch.zeros_like(im_blob)
        im_blob_ori = im_blob.clone().data
        outputs = outputs_ori
        H, W = outputs_ori['hm'].size()[2:]

        hm_index = inds[0][remain_inds]
        hm_index_att = hm_index[attack_ind].item()
        index = list(range(hm_index.size(0)))
        index.pop(attack_ind)

        wh_ori = outputs['wh'].clone().data
        reg_ori = outputs['reg'].clone().data

        i = 0
        while True:
            i += 1
            loss = 0

            hm_index_att_lst = [hm_index_att]

            loss -= ((outputs['hm'].view(-1)[hm_index_att_lst].sigmoid()) ** 2).mean()
            if ad_bbox:
                assert track_v is not None
                hm_index_gen = hm_index_att_lst[0]
                hm_index_gen += -(np.sign(track_v[0]) + W * np.sign(track_v[1]))
                loss -= ((1 - outputs['hm'].view(-1)[[hm_index_gen]].sigmoid()) ** 2).mean()
                loss -= smoothL1(outputs['wh'].view(2, -1)[:, [hm_index_gen]].T,
                                 wh_ori.view(2, -1)[:, hm_index_att_lst].T)
                loss -= smoothL1(outputs['reg'].view(2, -1)[:, [hm_index_gen]].T,
                                 reg_ori.view(2, -1)[:, hm_index_att_lst].T)

            loss.backward()

            grad = im_blob.grad
            grad /= (grad ** 2).sum().sqrt() + 1e-8

            noise += grad * 2

            im_blob = torch.clip(im_blob_ori + noise, min=0, max=1).data
            outputs, suc, _ = self.forwardFeatureDet(
                im_blob,
                img0,
                dets,
                [attack_ind],
                thr=1 if ad_bbox else 0,
                vs=[track_v] if ad_bbox else []
            )
            if suc:
                break

            if i > 60:
                break

        return noise, i, suc

    def attack_sg_det(
            self,
            im_blob,
            img0,
            dets,
            inds,
            remain_inds,
            last_info,
            outputs_ori,
            attack_id,
            attack_ind
    ):
        noise = torch.zeros_like(im_blob)
        im_blob_ori = im_blob.clone().data
        outputs = outputs_ori
        H, W = outputs_ori['hm'].size()[2:]

        hm_index = inds[0][remain_inds]
        hm_index_att = hm_index[attack_ind].item()
        index = list(range(hm_index.size(0)))
        index.pop(attack_ind)

        i = 0
        while True:
            i += 1
            loss = 0

            hm_index_att_lst = [hm_index_att]
            # for n_i in range(3):
            #     for n_j in range(3):
            #         hm_index_att_ = hm_index_att + (n_i - 1) * W + (n_j - 1)
            #         hm_index_att_ = max(0, min(H * W - 1, hm_index_att_))
            #         hm_index_att_lst.append(hm_index_att_)

            loss -= ((outputs['hm'].view(-1)[hm_index_att_lst].sigmoid()) ** 2).mean()
            # loss += ((outputs['hm'].view(-1)[hm_index_att_lst].sigmoid()) ** 2 *
            #          torch.log(1 - outputs['hm'].view(-1)[hm_index_att_lst].sigmoid())).mean()

            loss.backward()

            grad = im_blob.grad
            grad /= (grad ** 2).sum().sqrt() + 1e-8

            noise += grad * 2

            im_blob = torch.clip(im_blob_ori + noise, min=0, max=1).data
            outputs, suc, _ = self.forwardFeatureDet(
                im_blob,
                img0,
                dets,
                [attack_ind]
            )
            if suc:
                break

            if i > 60:
                break

        return noise, i, suc

    def attack_mt_hj(
            self,
            im_blob,
            img0,
            dets,
            inds,
            remain_inds,
            last_info,
            outputs_ori,
            attack_ids,
            attack_inds,
            ad_ids,
            track_vs
    ):
        img0_h, img0_w = img0.shape[:2]
        H, W = outputs_ori['hm'].size()[2:]
        r_w, r_h = img0_w / W, img0_h / H
        r_max = max(r_w, r_h)
        noise = torch.zeros_like(im_blob)
        im_blob_ori = im_blob.clone().data
        outputs = outputs_ori
        wh_ori = outputs['wh'].clone().data
        reg_ori = outputs['reg'].clone().data
        i = 0
        hm_index = inds[0][remain_inds]
        hm_index_att_lst = hm_index[attack_inds].cpu().numpy().tolist()

        best_i = None
        best_noise = None
        best_fail = np.inf
        while True:
            i += 1
            loss = 0

            loss -= ((outputs['hm'].view(-1)[hm_index_att_lst].sigmoid()) ** 2).mean()

            hm_index_att_lst_ = [hm_index_att_lst[j] for j in range(len(hm_index_att_lst))
                                 if attack_ids[j] not in ad_ids]

            if len(hm_index_att_lst_):
                assert len(track_vs) == len(hm_index_att_lst_)
                hm_index_gen_lst = []
                for index in range(len(hm_index_att_lst_)):
                    track_v = track_vs[index]
                    hm_index_gen = hm_index_att_lst_[index]
                    hm_index_gen += -(np.sign(track_v[0]) + W * np.sign(track_v[1]))
                    hm_index_gen_lst.append(hm_index_gen)
                loss -= ((1 - outputs['hm'].view(-1)[hm_index_gen_lst].sigmoid()) ** 2).mean()
                loss -= smoothL1(outputs['wh'].view(2, -1)[:, hm_index_gen_lst].T,
                                 wh_ori.view(2, -1)[:, hm_index_att_lst_].T)
                loss -= smoothL1(outputs['reg'].view(2, -1)[:, hm_index_gen_lst].T,
                                 reg_ori.view(2, -1)[:, hm_index_att_lst_].T)

            loss.backward()

            grad = im_blob.grad
            grad /= (grad ** 2).sum().sqrt() + 1e-8

            noise += grad

            thrs = [0 for j in range(len(attack_inds))]
            for j in range(len(thrs)):
                if attack_ids[j] not in ad_ids:
                    thrs[j] = 0.9

            im_blob = torch.clip(im_blob_ori + noise, min=0, max=1).data
            outputs, suc, fail_ids = self.forwardFeatureDet(
                im_blob,
                img0,
                dets,
                attack_inds.tolist(),
                thr=thrs
            )

            if fail_ids is not None:
                if fail_ids == 0:
                    break
                elif fail_ids <= best_fail:
                    best_fail = fail_ids
                    best_i = i
                    best_noise = noise.clone()
            if i > 60:
                if self.opt.no_f_noise:
                    return None, i, False
                else:
                    if best_i is not None:
                        noise = best_noise
                        i = best_i
                    return noise, i, False
        return noise, i, True

    def attack_mt_det(
            self,
            im_blob,
            img0,
            dets,
            inds,
            remain_inds,
            last_info,
            outputs_ori,
            attack_ids,
            attack_inds
    ):
        img0_h, img0_w = img0.shape[:2]
        H, W = outputs_ori['hm'].size()[2:]
        r_w, r_h = img0_w / W, img0_h / H
        r_max = max(r_w, r_h)
        noise = torch.zeros_like(im_blob)
        im_blob_ori = im_blob.clone().data
        outputs = outputs_ori
        wh_ori = outputs['wh'].clone().data
        reg_ori = outputs['reg'].clone().data
        i = 0
        hm_index = inds[0][remain_inds]
        hm_index_att_lst = hm_index[attack_inds].cpu().numpy().tolist()

        best_i = None
        best_noise = None
        best_fail = np.inf
        while True:
            i += 1
            loss = 0

            loss -= ((outputs['hm'].view(-1)[hm_index_att_lst].sigmoid()) ** 2).mean()

            loss.backward()

            grad = im_blob.grad
            grad /= (grad ** 2).sum().sqrt() + 1e-8

            noise += grad

            im_blob = torch.clip(im_blob_ori + noise, min=0, max=1).data
            outputs, suc, fail_ids = self.forwardFeatureDet(
                im_blob,
                img0,
                dets,
                attack_inds.tolist()
            )

            if fail_ids is not None:
                if fail_ids == 0:
                    break
                elif fail_ids <= best_fail:
                    best_fail = fail_ids
                    best_i = i
                    best_noise = noise.clone()
            if i > 60:
                if self.opt.no_f_noise:
                    return None, i, False
                else:
                    if best_i is not None:
                        noise = best_noise
                        i = best_i
                    return noise, i, False
        return noise, i, True

    def attack_sg_feat(
            self,
            im_blob,
            img0,
            id_features,
            dets,
            inds,
            remain_inds,
            last_info,
            outputs_ori,
            attack_id,
            attack_ind,
            target_id,
            target_ind
    ):
        noise = torch.zeros_like(im_blob)
        im_blob_ori = im_blob.clone().data

        last_ad_id_features = [None for _ in range(len(id_features[0]))]
        for i in range(len(id_features)):
            id_features[i] = id_features[i][[attack_ind, target_ind]]

        i = 0
        suc = True
        while True:
            i += 1
            loss = 0
            loss_feat = 0
            for id_i, id_feature in enumerate(id_features):
                if last_ad_id_features[attack_ind] is not None:
                    last_ad_id_feature = torch.from_numpy(last_ad_id_features[attack_ind]).unsqueeze(0).cuda()
                    sim_1 = torch.mm(id_feature[0:0 + 1], last_ad_id_feature.T).squeeze()
                    sim_2 = torch.mm(id_feature[1:1 + 1], last_ad_id_feature.T).squeeze()
                    loss_feat += sim_2 - sim_1
                if last_ad_id_features[target_ind] is not None:
                    last_ad_id_feature = torch.from_numpy(last_ad_id_features[target_ind]).unsqueeze(0).cuda()
                    sim_1 = torch.mm(id_feature[1:1 + 1], last_ad_id_feature.T).squeeze()
                    sim_2 = torch.mm(id_feature[0:0 + 1], last_ad_id_feature.T).squeeze()
                    loss_feat += sim_2 - sim_1
                if last_ad_id_features[attack_ind] is None and last_ad_id_features[target_ind] is None:
                    loss_feat += torch.mm(id_feature[0:0 + 1], id_feature[1:1 + 1].T).squeeze()
            loss += loss_feat / len(id_features)

            loss.backward()

            grad = im_blob.grad
            grad /= (grad ** 2).sum().sqrt() + 1e-8

            noise += grad

            im_blob = torch.clip(im_blob_ori + noise, min=0, max=1).data
            id_features_, outputs_, ae_attack_id, ae_target_id, hm_index_ = self.forwardFeatureSg(
                im_blob,
                img0,
                dets,
                inds,
                remain_inds,
                attack_id,
                attack_ind,
                target_id,
                target_ind,
                last_info
            )
            if id_features_ is not None:
                id_features = id_features_

            if ae_attack_id != attack_id and ae_attack_id is not None:
                break

            if i > 60:
                suc = False
                break
        return noise, i, suc

    def attack_sg_cl(
            self,
            im_blob,
            img0,
            id_features,
            dets,
            inds,
            remain_inds,
            last_info,
            outputs_ori,
            attack_id,
            attack_ind,
            target_id,
            target_ind
    ):
        img0_h, img0_w = img0.shape[:2]
        H, W = outputs_ori['hm'].size()[2:]
        r_w, r_h = img0_w / W, img0_h / H
        r_max = max(r_w, r_h)
        noise = torch.zeros_like(im_blob)
        im_blob_ori = im_blob.clone().data
        outputs = outputs_ori
        wh_ori = outputs['wh'].clone().data
        reg_ori = outputs['reg'].clone().data

        last_ad_id_features = [None for _ in range(len(id_features[0]))]
        strack_pool = copy.deepcopy(last_info['last_strack_pool'])
        last_attack_det = None
        last_target_det = None
        STrack.multi_predict(strack_pool)
        for strack in strack_pool:
            if strack.track_id == attack_id:
                last_ad_id_features[attack_ind] = strack.smooth_feat
                last_attack_det = torch.from_numpy(strack.tlbr).cuda().float()
                last_attack_det[[0, 2]] = (last_attack_det[[0, 2]] - 0.5 * W * (r_w - r_max)) / r_max
                last_attack_det[[1, 3]] = (last_attack_det[[1, 3]] - 0.5 * H * (r_h - r_max)) / r_max
            elif strack.track_id == target_id:
                last_ad_id_features[target_ind] = strack.smooth_feat
                last_target_det = torch.from_numpy(strack.tlbr).cuda().float()
                last_target_det[[0, 2]] = (last_target_det[[0, 2]] - 0.5 * W * (r_w - r_max)) / r_max
                last_target_det[[1, 3]] = (last_target_det[[1, 3]] - 0.5 * H * (r_h - r_max)) / r_max
        last_attack_det_center = torch.round(
            (last_attack_det[:2] + last_attack_det[2:]) / 2) if last_attack_det is not None else None
        last_target_det_center = torch.round(
            (last_target_det[:2] + last_target_det[2:]) / 2) if last_target_det is not None else None

        hm_index = inds[0][remain_inds]

        for i in range(len(id_features)):
            id_features[i] = id_features[i][[attack_ind, target_ind]]

        i = 0
        j = -1
        suc = True
        ori_hm_index = hm_index[[attack_ind, target_ind]].clone()
        ori_hm_index_re = hm_index[[target_ind, attack_ind]].clone()
        att_hm_index = None
        noise_0 = None
        i_0 = None
        noise_1 = None
        i_1 = None
        while True:
            i += 1
            loss = 0
            loss_feat = 0
            # for id_i, id_feature in enumerate(id_features):
            #     if last_ad_id_features[attack_ind] is not None:
            #         last_ad_id_feature = torch.from_numpy(last_ad_id_features[attack_ind]).unsqueeze(0).cuda()
            #         sim_1 = torch.mm(id_feature[0:0 + 1], last_ad_id_feature.T).squeeze()
            #         sim_2 = torch.mm(id_feature[1:1 + 1], last_ad_id_feature.T).squeeze()
            #         loss_feat += sim_2 - sim_1
            #     if last_ad_id_features[target_ind] is not None:
            #         last_ad_id_feature = torch.from_numpy(last_ad_id_features[target_ind]).unsqueeze(0).cuda()
            #         sim_1 = torch.mm(id_feature[1:1 + 1], last_ad_id_feature.T).squeeze()
            #         sim_2 = torch.mm(id_feature[0:0 + 1], last_ad_id_feature.T).squeeze()
            #         loss_feat += sim_2 - sim_1
            #     if last_ad_id_features[attack_ind] is None and last_ad_id_features[target_ind] is None:
            #         loss_feat += torch.mm(id_feature[0:0 + 1], id_feature[1:1 + 1].T).squeeze()
            # loss += loss_feat / len(id_features)

            if i in [1, 10, 20, 30, 35, 40, 45, 50, 55]:
                attack_det_center = torch.stack([hm_index[attack_ind] % W, hm_index[attack_ind] // W]).float()
                target_det_center = torch.stack([hm_index[target_ind] % W, hm_index[target_ind] // W]).float()
                if last_target_det_center is not None:
                    attack_center_delta = attack_det_center - last_target_det_center
                    if torch.max(torch.abs(attack_center_delta)) > 1:
                        attack_center_delta /= torch.max(torch.abs(attack_center_delta))
                        attack_det_center = torch.round(attack_det_center - attack_center_delta).int()
                        hm_index[attack_ind] = attack_det_center[0] + attack_det_center[1] * W
                if last_attack_det_center is not None:
                    target_center_delta = target_det_center - last_attack_det_center
                    if torch.max(torch.abs(target_center_delta)) > 1:
                        target_center_delta /= torch.max(torch.abs(target_center_delta))
                        target_det_center = torch.round(target_det_center - target_center_delta).int()
                        hm_index[target_ind] = target_det_center[0] + target_det_center[1] * W
                att_hm_index = hm_index[[attack_ind, target_ind]].clone()

            if att_hm_index is not None:
                n_att_hm_index = []
                n_ori_hm_index_re = []
                for hm_ind in range(len(att_hm_index)):
                    for n_i in range(3):
                        for n_j in range(3):
                            att_hm_ind = att_hm_index[hm_ind].item()
                            att_hm_ind = att_hm_ind + (n_i - 1) * W + (n_j - 1)
                            att_hm_ind = max(0, min(H*W-1, att_hm_ind))
                            n_att_hm_index.append(att_hm_ind)
                            ori_hm_ind = ori_hm_index_re[hm_ind].item()
                            ori_hm_ind = ori_hm_ind + (n_i - 1) * W + (n_j - 1)
                            ori_hm_ind = max(0, min(H * W - 1, ori_hm_ind))
                            n_ori_hm_index_re.append(ori_hm_ind)
                # print(n_att_hm_index, n_ori_hm_index_re)
                loss += ((1 - outputs['hm'].view(-1).sigmoid()[n_att_hm_index]) ** 2 *
                         torch.log(outputs['hm'].view(-1).sigmoid()[n_att_hm_index])).mean()
                loss += ((outputs['hm'].view(-1).sigmoid()[n_ori_hm_index_re]) ** 2 *
                         torch.log(1 - outputs['hm'].view(-1).sigmoid()[n_ori_hm_index_re])).mean()
                loss -= smoothL1(outputs['wh'].view(2, -1)[:, n_att_hm_index].T, wh_ori.view(2, -1)[:, n_ori_hm_index_re].T)
                loss -= smoothL1(outputs['reg'].view(2, -1)[:, n_att_hm_index].T, reg_ori.view(2, -1)[:, n_ori_hm_index_re].T)

            loss.backward()

            grad = im_blob.grad
            grad /= (grad ** 2).sum().sqrt() + 1e-8

            noise += grad

            im_blob = torch.clip(im_blob_ori + noise, min=0, max=1).data
            id_features_, outputs_, ae_attack_id, ae_target_id, hm_index_ = self.forwardFeatureSg(
                im_blob,
                img0,
                dets,
                inds,
                remain_inds,
                attack_id,
                attack_ind,
                target_id,
                target_ind,
                last_info
            )
            if id_features_ is not None:
                id_features = id_features_
            if outputs_ is not None:
                outputs = outputs_
            # if hm_index_ is not None:
            #     hm_index = hm_index_
            if ae_attack_id != attack_id and ae_attack_id is not None:
                break

            if i > 60:
                if noise_0 is not None:
                    return noise_0, i_0, suc
                elif noise_1 is not None:
                    return noise_1, i_1, suc
                if self.opt.no_f_noise:
                    return None, i, False
                else:
                    suc = False
                    break
        return noise, i, suc

    def attack_sg_random(
            self,
            im_blob,
            img0,
            id_features,
            dets,
            inds,
            remain_inds,
            last_info,
            outputs_ori,
            attack_id,
            attack_ind,
            target_id,
            target_ind
    ):
        im_blob_ori = im_blob.clone().data

        suc = False

        noise = torch.rand(im_blob_ori.size()).to(im_blob_ori.device)
        noise /= (noise**2).sum().sqrt()
        noise *= random.uniform(2, 8)

        im_blob = torch.clip(im_blob_ori + noise, min=0, max=1).data
        id_features_, outputs_, ae_attack_id, ae_target_id, hm_index_ = self.forwardFeatureSg(
            im_blob,
            img0,
            dets,
            inds,
            remain_inds,
            attack_id,
            attack_ind,
            target_id,
            target_ind,
            last_info,
            grad=False
        )

        if ae_attack_id != attack_id and ae_attack_id is not None:
            suc = True

        return noise, 1, suc

    def attack_mt_random(
            self,
            im_blob,
            img0,
            id_features,
            dets,
            inds,
            remain_inds,
            last_info,
            outputs_ori,
            attack_ids,
            attack_inds,
            target_ids,
            target_inds
    ):
        im_blob_ori = im_blob.clone().data

        suc = False

        noise = torch.rand(im_blob_ori.size()).to(im_blob_ori.device)
        noise /= (noise ** 2).sum().sqrt()
        noise *= random.uniform(2, 8)

        im_blob = torch.clip(im_blob_ori + noise, min=0, max=1).data
        id_features, outputs, fail_ids = self.forwardFeatureMt(
            im_blob,
            img0,
            dets,
            inds,
            remain_inds,
            attack_ids,
            attack_inds,
            target_ids,
            target_inds,
            last_info,
            grad=False
        )
        if fail_ids == 0:
            suc = True

        return noise, 1, suc

    def attack_sg(
            self,
            im_blob,
            img0,
            id_features,
            dets,
            inds,
            remain_inds,
            last_info,
            outputs_ori,
            attack_id,
            attack_ind,
            target_id,
            target_ind
    ):
        img0_h, img0_w = img0.shape[:2]
        H, W = outputs_ori['hm'].size()[2:]
        r_w, r_h = img0_w / W, img0_h / H
        r_max = max(r_w, r_h)
        noise = torch.zeros_like(im_blob)
        im_blob_ori = im_blob.clone().data
        outputs = outputs_ori
        wh_ori = outputs['wh'].clone().data
        reg_ori = outputs['reg'].clone().data

        last_ad_id_features = [None for _ in range(len(id_features[0]))]
        strack_pool = copy.deepcopy(last_info['last_strack_pool'])
        last_attack_det = None
        last_target_det = None
        STrack.multi_predict(strack_pool)
        for strack in strack_pool:
            if strack.track_id == attack_id:
                last_ad_id_features[attack_ind] = strack.smooth_feat
                last_attack_det = torch.from_numpy(strack.tlbr).cuda().float()
                last_attack_det[[0, 2]] = (last_attack_det[[0, 2]] - 0.5 * W * (r_w - r_max)) / r_max
                last_attack_det[[1, 3]] = (last_attack_det[[1, 3]] - 0.5 * H * (r_h - r_max)) / r_max
            elif strack.track_id == target_id:
                last_ad_id_features[target_ind] = strack.smooth_feat
                last_target_det = torch.from_numpy(strack.tlbr).cuda().float()
                last_target_det[[0, 2]] = (last_target_det[[0, 2]] - 0.5 * W * (r_w - r_max)) / r_max
                last_target_det[[1, 3]] = (last_target_det[[1, 3]] - 0.5 * H * (r_h - r_max)) / r_max
        last_attack_det_center = torch.round(
            (last_attack_det[:2] + last_attack_det[2:]) / 2) if last_attack_det is not None else None
        last_target_det_center = torch.round(
            (last_target_det[:2] + last_target_det[2:]) / 2) if last_target_det is not None else None

        hm_index = inds[0][remain_inds]

        for i in range(len(id_features)):
            id_features[i] = id_features[i][[attack_ind, target_ind]]

        i = 0
        j = -1
        suc = True
        ori_hm_index = hm_index[[attack_ind, target_ind]].clone()
        ori_hm_index_re = hm_index[[target_ind, attack_ind]].clone()
        att_hm_index = None
        noise_0 = None
        i_0 = None
        noise_1 = None
        i_1 = None
        while True:
            i += 1
            loss = 0
            loss_feat = 0
            for id_i, id_feature in enumerate(id_features):
                if last_ad_id_features[attack_ind] is not None:
                    last_ad_id_feature = torch.from_numpy(last_ad_id_features[attack_ind]).unsqueeze(0).cuda()
                    sim_1 = torch.mm(id_feature[0:0 + 1], last_ad_id_feature.T).squeeze()
                    sim_2 = torch.mm(id_feature[1:1 + 1], last_ad_id_feature.T).squeeze()
                    loss_feat += sim_2 - sim_1
                if last_ad_id_features[target_ind] is not None:
                    last_ad_id_feature = torch.from_numpy(last_ad_id_features[target_ind]).unsqueeze(0).cuda()
                    sim_1 = torch.mm(id_feature[1:1 + 1], last_ad_id_feature.T).squeeze()
                    sim_2 = torch.mm(id_feature[0:0 + 1], last_ad_id_feature.T).squeeze()
                    loss_feat += sim_2 - sim_1
                if last_ad_id_features[attack_ind] is None and last_ad_id_features[target_ind] is None:
                    loss_feat += torch.mm(id_feature[0:0 + 1], id_feature[1:1 + 1].T).squeeze()
            loss += loss_feat / len(id_features)

            if i in [10, 20, 30, 35, 40, 45, 50, 55]:
                attack_det_center = torch.stack([hm_index[attack_ind] % W, hm_index[attack_ind] // W]).float()
                target_det_center = torch.stack([hm_index[target_ind] % W, hm_index[target_ind] // W]).float()
                if last_target_det_center is not None:
                    attack_center_delta = attack_det_center - last_target_det_center
                    if torch.max(torch.abs(attack_center_delta)) > 1:
                        attack_center_delta /= torch.max(torch.abs(attack_center_delta))
                        attack_det_center = torch.round(attack_det_center - attack_center_delta).int()
                        hm_index[attack_ind] = attack_det_center[0] + attack_det_center[1] * W
                if last_attack_det_center is not None:
                    target_center_delta = target_det_center - last_attack_det_center
                    if torch.max(torch.abs(target_center_delta)) > 1:
                        target_center_delta /= torch.max(torch.abs(target_center_delta))
                        target_det_center = torch.round(target_det_center - target_center_delta).int()
                        hm_index[target_ind] = target_det_center[0] + target_det_center[1] * W
                att_hm_index = hm_index[[attack_ind, target_ind]].clone()

            if att_hm_index is not None:
                n_att_hm_index = []
                n_ori_hm_index_re = []
                for hm_ind in range(len(att_hm_index)):
                    for n_i in range(3):
                        for n_j in range(3):
                            att_hm_ind = att_hm_index[hm_ind].item()
                            att_hm_ind = att_hm_ind + (n_i - 1) * W + (n_j - 1)
                            att_hm_ind = max(0, min(H*W-1, att_hm_ind))
                            n_att_hm_index.append(att_hm_ind)
                            ori_hm_ind = ori_hm_index_re[hm_ind].item()
                            ori_hm_ind = ori_hm_ind + (n_i - 1) * W + (n_j - 1)
                            ori_hm_ind = max(0, min(H * W - 1, ori_hm_ind))
                            n_ori_hm_index_re.append(ori_hm_ind)
                # print(n_att_hm_index, n_ori_hm_index_re)
                loss += ((1 - outputs['hm'].view(-1).sigmoid()[n_att_hm_index]) ** 2 *
                         torch.log(outputs['hm'].view(-1).sigmoid()[n_att_hm_index])).mean()
                loss += ((outputs['hm'].view(-1).sigmoid()[n_ori_hm_index_re]) ** 2 *
                         torch.log(1 - outputs['hm'].view(-1).sigmoid()[n_ori_hm_index_re])).mean()
                loss -= smoothL1(outputs['wh'].view(2, -1)[:, n_att_hm_index].T, wh_ori.view(2, -1)[:, n_ori_hm_index_re].T)
                loss -= smoothL1(outputs['reg'].view(2, -1)[:, n_att_hm_index].T, reg_ori.view(2, -1)[:, n_ori_hm_index_re].T)

            loss.backward()

            grad = im_blob.grad
            grad /= (grad ** 2).sum().sqrt() + 1e-8

            noise += grad

            im_blob = torch.clip(im_blob_ori + noise, min=0, max=1).data
            id_features_, outputs_, ae_attack_id, ae_target_id, hm_index_ = self.forwardFeatureSg(
                im_blob,
                img0,
                dets,
                inds,
                remain_inds,
                attack_id,
                attack_ind,
                target_id,
                target_ind,
                last_info
            )
            if id_features_ is not None:
                id_features = id_features_
            if outputs_ is not None:
                outputs = outputs_
            # if hm_index_ is not None:
            #     hm_index = hm_index_
            if ae_attack_id != attack_id and ae_attack_id is not None:
                break

            if i > 60:
                if noise_0 is not None:
                    return noise_0, i_0, suc
                elif noise_1 is not None:
                    return noise_1, i_1, suc
                if self.opt.no_f_noise:
                    return None, i, False
                else:
                    suc = False
                    break
        return noise, i, suc

    def attack_mt(
            self,
            im_blob,
            img0,
            id_features,
            dets,
            inds,
            remain_inds,
            last_info,
            outputs_ori,
            attack_ids,
            attack_inds,
            target_ids,
            target_inds
    ):
        img0_h, img0_w = img0.shape[:2]
        H, W = outputs_ori['hm'].size()[2:]
        r_w, r_h = img0_w / W, img0_h / H
        r_max = max(r_w, r_h)
        noise = torch.zeros_like(im_blob)
        im_blob_ori = im_blob.clone().data
        outputs = outputs_ori
        wh_ori = outputs['wh'].clone().data
        reg_ori = outputs['reg'].clone().data
        i = 0
        j = -1
        last_ad_id_features = [None for _ in range(len(id_features[0]))]
        strack_pool = copy.deepcopy(last_info['last_strack_pool'])
        ad_attack_ids = [self.multiple_ori2att[attack_id] for attack_id in attack_ids]
        ad_target_ids = [self.multiple_ori2att[target_id] for target_id in target_ids]
        last_attack_dets = [None] * len(ad_attack_ids)
        last_target_dets = [None] * len(ad_target_ids)
        STrack.multi_predict(strack_pool)
        for strack in strack_pool:
            if strack.track_id in ad_attack_ids:
                index = ad_attack_ids.index(strack.track_id)
                last_ad_id_features[attack_inds[index]] = strack.smooth_feat
                last_attack_dets[index] = torch.from_numpy(strack.tlbr).cuda().float()
                last_attack_dets[index][[0, 2]] = (last_attack_dets[index][[0, 2]] - 0.5 * W * (r_w - r_max)) / r_max
                last_attack_dets[index][[1, 3]] = (last_attack_dets[index][[1, 3]] - 0.5 * H * (r_h - r_max)) / r_max
            if strack.track_id in ad_target_ids:
                index = ad_target_ids.index(strack.track_id)
                last_ad_id_features[target_inds[index]] = strack.smooth_feat
                last_target_dets[index] = torch.from_numpy(strack.tlbr).cuda().float()
                last_target_dets[index][[0, 2]] = (last_target_dets[index][[0, 2]] - 0.5 * W * (r_w - r_max)) / r_max
                last_target_dets[index][[1, 3]] = (last_target_dets[index][[1, 3]] - 0.5 * H * (r_h - r_max)) / r_max

        last_attack_dets_center = []
        for det in last_attack_dets:
            if det is None:
                last_attack_dets_center.append(None)
            else:
                last_attack_dets_center.append((det[:2] + det[2:]) / 2)
        last_target_dets_center = []
        for det in last_target_dets:
            if det is None:
                last_target_dets_center.append(None)
            else:
                last_target_dets_center.append((det[:2] + det[2:]) / 2)

        hm_index = inds[0][remain_inds]

        ori_hm_index_re_lst = []
        for ind in range(len(attack_ids)):
            attack_ind = attack_inds[ind]
            target_ind = target_inds[ind]
            ori_hm_index_re_lst.append(hm_index[[target_ind, attack_ind]].clone())
        att_hm_index_lst = []
        best_i = None
        best_noise = None
        best_fail = np.inf
        while True:
            i += 1
            loss = 0
            loss_feat = 0
            for index, attack_id in enumerate(attack_ids):
                target_id = target_ids[index]
                attack_ind = attack_inds[index]
                target_ind = target_inds[index]
                for id_i, id_feature in enumerate(id_features):
                    if last_ad_id_features[attack_ind] is not None:
                        last_ad_id_feature = torch.from_numpy(last_ad_id_features[attack_ind]).unsqueeze(0).cuda()
                        sim_1 = torch.mm(id_feature[attack_ind:attack_ind + 1], last_ad_id_feature.T).squeeze()
                        sim_2 = torch.mm(id_feature[target_ind:target_ind + 1], last_ad_id_feature.T).squeeze()
                        if self.opt.hard_sample > 0:
                            loss_feat += torch.clamp(sim_2 - sim_1, max=self.opt.hard_sample)
                        else:
                            loss_feat += sim_2 - sim_1
                    if last_ad_id_features[target_ind] is not None:
                        last_ad_id_feature = torch.from_numpy(last_ad_id_features[target_ind]).unsqueeze(0).cuda()
                        sim_1 = torch.mm(id_feature[target_ind:target_ind + 1], last_ad_id_feature.T).squeeze()
                        sim_2 = torch.mm(id_feature[attack_ind:attack_ind + 1], last_ad_id_feature.T).squeeze()
                        if self.opt.hard_sample > 0:
                            loss_feat += torch.clamp(sim_2 - sim_1, max=self.opt.hard_sample)
                        else:
                            loss_feat += sim_2 - sim_1
                    if last_ad_id_features[attack_ind] is None and last_ad_id_features[target_ind] is None:
                        loss_feat += torch.mm(id_feature[attack_ind:attack_ind + 1],
                                              id_feature[target_ind:target_ind + 1].T).squeeze()

                if i in [10, 20, 30, 35, 40, 45, 50, 55]:
                    attack_det_center = torch.stack([hm_index[attack_ind] % W, hm_index[attack_ind] // W]).float()
                    target_det_center = torch.stack([hm_index[target_ind] % W, hm_index[target_ind] // W]).float()
                    if last_target_dets_center[index] is not None:
                        attack_center_delta = attack_det_center - last_target_dets_center[index]
                        if torch.max(torch.abs(attack_center_delta)) > 1:
                            attack_center_delta /= torch.max(torch.abs(attack_center_delta))
                            attack_det_center = torch.round(attack_det_center - attack_center_delta).int()
                            hm_index[attack_ind] = attack_det_center[0] + attack_det_center[1] * W
                    if last_attack_dets_center[index] is not None:
                        target_center_delta = target_det_center - last_attack_dets_center[index]
                        if torch.max(torch.abs(target_center_delta)) > 1:
                            target_center_delta /= torch.max(torch.abs(target_center_delta))
                            target_det_center = torch.round(target_det_center - target_center_delta).int()
                            hm_index[target_ind] = target_det_center[0] + target_det_center[1] * W
                    if index == 0:
                        att_hm_index_lst = []
                    att_hm_index_lst.append(hm_index[[attack_ind, target_ind]].clone())

            loss += loss_feat / len(id_features)

            if len(att_hm_index_lst):
                assert len(att_hm_index_lst) == len(ori_hm_index_re_lst)
                n_att_hm_index_lst = []
                n_ori_hm_index_re_lst = []
                for lst_ind in range(len(att_hm_index_lst)):
                    for hm_ind in range(len(att_hm_index_lst[lst_ind])):
                        for n_i in range(3):
                            for n_j in range(3):
                                att_hm_ind = att_hm_index_lst[lst_ind][hm_ind].item()
                                att_hm_ind = att_hm_ind + (n_i - 1) * W + (n_j - 1)
                                att_hm_ind = max(0, min(H*W-1, att_hm_ind))
                                n_att_hm_index_lst.append(att_hm_ind)
                                ori_hm_ind = ori_hm_index_re_lst[lst_ind][hm_ind].item()
                                ori_hm_ind = ori_hm_ind + (n_i - 1) * W + (n_j - 1)
                                ori_hm_ind = max(0, min(H * W - 1, ori_hm_ind))
                                n_ori_hm_index_re_lst.append(ori_hm_ind)
                # print(n_att_hm_index, n_ori_hm_index_re)
                loss += ((1 - outputs['hm'].view(-1).sigmoid()[n_att_hm_index_lst]) ** 2 *
                         torch.log(outputs['hm'].view(-1).sigmoid()[n_att_hm_index_lst])).mean()
                loss += ((outputs['hm'].view(-1).sigmoid()[n_ori_hm_index_re_lst]) ** 2 *
                         torch.log(1 - outputs['hm'].view(-1).sigmoid()[n_ori_hm_index_re_lst])).mean()
                loss -= smoothL1(outputs['wh'].view(2, -1)[:, n_att_hm_index_lst].T, wh_ori.view(2, -1)[:, n_ori_hm_index_re_lst].T)
                loss -= smoothL1(outputs['reg'].view(2, -1)[:, n_att_hm_index_lst].T, reg_ori.view(2, -1)[:, n_ori_hm_index_re_lst].T)

            loss.backward()

            grad = im_blob.grad
            grad /= (grad ** 2).sum().sqrt() + 1e-8

            noise += grad

            im_blob = torch.clip(im_blob_ori + noise, min=0, max=1).data
            id_features, outputs, fail_ids = self.forwardFeatureMt(
                im_blob,
                img0,
                dets,
                inds,
                remain_inds,
                attack_ids,
                attack_inds,
                target_ids,
                target_inds,
                last_info
            )
            if fail_ids is not None:
                if fail_ids == 0:
                    break
                elif fail_ids <= best_fail:
                    best_fail = fail_ids
                    best_i = i
                    best_noise = noise.clone()
            if i > 60:
                if self.opt.no_f_noise:
                    return None, i, False
                else:
                    if best_i is not None:
                        noise = best_noise
                        i = best_i
                    return noise, i, False
        return noise, i, True

    def forwardFeatureDet(self, im_blob, img0, dets_, attack_inds, thr=0, vs=[]):
        width = img0.shape[1]
        height = img0.shape[0]
        inp_height = im_blob.shape[2]
        inp_width = im_blob.shape[3]
        c = np.array([width / 2., height / 2.], dtype=np.float32)
        s = max(float(inp_width) / float(inp_height) * height, width) * 1.0
        meta = {'c': c, 's': s,
                'out_height': inp_height // self.opt.down_ratio,
                'out_width': inp_width // self.opt.down_ratio}

        im_blob.requires_grad = True
        self.model.zero_grad()
        output = self.model(im_blob)[-1]
        hm = output['hm'].sigmoid()
        wh = output['wh']

        reg = output['reg'] if self.opt.reg_offset else None
        dets_raw, inds = mot_decode(hm, wh, reg=reg, cat_spec_wh=self.opt.cat_spec_wh, K=self.opt.K)

        dets = self.post_process(dets_raw.clone(), meta)
        dets = self.merge_outputs([dets])[1]

        remain_inds = dets[:, 4] > self.opt.conf_thres
        dets = dets[remain_inds]

        ious = bbox_ious(np.ascontiguousarray(dets_[:, :4], dtype=np.float),
                         np.ascontiguousarray(dets[:, :4], dtype=np.float))
        row_inds, col_inds = linear_sum_assignment(-ious)

        if not isinstance(thr, list):
            thr = [thr for _ in range(len(attack_inds))]
        fail_n = 0
        for i in range(len(row_inds)):
            if row_inds[i] in attack_inds:
                if ious[row_inds[i], col_inds[i]] > thr[attack_inds.index(row_inds[i])]:
                    fail_n += 1
                elif len(vs):
                    d_o = dets_[row_inds[i], :4]
                    d_a = dets[col_inds[i], :4]
                    c_o = (d_o[[0, 1]] + d_o[[2, 3]]) / 2
                    c_a = (d_a[[0, 1]] + d_a[[2, 3]]) / 2
                    c_d = ((c_a - c_o) / 4).astype(np.int) * vs[0]
                    if c_d[0] >= 0 or c_d[1] >= 0:
                        fail_n += 1
        return output, fail_n == 0, fail_n


    def forwardFeatureSg(self, im_blob, img0, dets_, inds_, remain_inds_, attack_id, attack_ind, target_id, target_ind,
                         last_info, grad=True):
        width = img0.shape[1]
        height = img0.shape[0]
        inp_height = im_blob.shape[2]
        inp_width = im_blob.shape[3]
        c = np.array([width / 2., height / 2.], dtype=np.float32)
        s = max(float(inp_width) / float(inp_height) * height, width) * 1.0
        meta = {'c': c, 's': s,
                'out_height': inp_height // self.opt.down_ratio,
                'out_width': inp_width // self.opt.down_ratio}

        im_blob.requires_grad = True
        self.model.zero_grad()
        if grad:
            output = self.model(im_blob)[-1]
        else:
            with torch.no_grad():
                output = self.model(im_blob)[-1]
        hm = output['hm'].sigmoid()
        wh = output['wh']
        id_feature = output['id']
        id_feature = F.normalize(id_feature, dim=1)

        reg = output['reg'] if self.opt.reg_offset else None
        dets_raw, inds = mot_decode(hm, wh, reg=reg, cat_spec_wh=self.opt.cat_spec_wh, K=self.opt.K)

        dets = self.post_process(dets_raw.clone(), meta)
        dets = self.merge_outputs([dets])[1]

        remain_inds = dets[:, 4] > self.opt.conf_thres
        dets = dets[remain_inds]

        if target_ind is None:
            ious = bbox_ious(np.ascontiguousarray(dets_[[attack_ind], :4], dtype=np.float),
                             np.ascontiguousarray(dets[:, :4], dtype=np.float))
        else:
            ious = bbox_ious(np.ascontiguousarray(dets_[[attack_ind, target_ind], :4], dtype=np.float),
                             np.ascontiguousarray(dets[:, :4], dtype=np.float))
        # det_ind = np.argmax(ious, axis=1)
        row_inds, col_inds = linear_sum_assignment(-ious)

        match = True
        if target_ind is None:
            if ious[row_inds[0], col_inds[0]] < 0.8:
                dets = dets_
                inds = inds_
                remain_inds = remain_inds_
                match = False
        else:
            if len(col_inds) < 2 or ious[row_inds[0], col_inds[0]] < 0.6 or ious[row_inds[1], col_inds[1]] < 0.6:
                dets = dets_
                inds = inds_
                remain_inds = remain_inds_
                match = False
        # assert match
        id_features = []
        for i in range(3):
            for j in range(3):
                id_feature_exp = _tranpose_and_gather_feat_expand(id_feature, inds, bias=(i - 1, j - 1)).squeeze(0)
                id_features.append(id_feature_exp)

        for i in range(len(id_features)):
            id_features[i] = id_features[i][remain_inds]

        ae_attack_id = None
        ae_target_id = None

        if not match:
            for i in range(len(id_features)):
                if target_ind is not None:
                    id_features[i] = id_features[i][[attack_ind, target_ind]]
                else:
                    id_features[i] = id_features[i][[attack_ind]]
            return id_features, output, ae_attack_id, ae_target_id, None

        if row_inds[0] == 0:
            ae_attack_ind = col_inds[0]
            ae_target_ind = col_inds[1] if target_ind is not None else None
        else:
            ae_attack_ind = col_inds[1]
            ae_target_ind = col_inds[0] if target_ind is not None else None
        # ae_attack_ind = det_ind[0]
        # ae_target_ind = det_ind[1] if target_ind is not None else None

        hm_index = None
        # if target_ind is not None:
        #     hm_index[[attack_ind, target_ind]] = hm_index[[ae_attack_ind, ae_target_ind]]

        id_features_ = [None for _ in range(len(id_features))]
        for i in range(len(id_features)):
            if target_ind is None:
                id_features_[i] = id_features[i][[ae_attack_ind]]
            else:
                try:
                    id_features_[i] = id_features[i][[ae_attack_ind, ae_target_ind]]
                except:
                    import pdb; pdb.set_trace()

        id_feature = _tranpose_and_gather_feat_expand(id_feature, inds)
        id_feature = id_feature.squeeze(0)
        id_feature = id_feature[remain_inds]
        id_feature = id_feature.detach().cpu().numpy()

        if len(dets) > 0:
            '''Detections'''
            detections = [STrack(STrack.tlbr_to_tlwh(tlbrs[:4]), tlbrs[4], f, 30) for
                          (tlbrs, f) in zip(dets[:, :5], id_feature)]
        else:
            detections = []

        unconfirmed = copy.deepcopy(last_info['last_unconfirmed'])
        strack_pool = copy.deepcopy(last_info['last_strack_pool'])
        kalman_filter = copy.deepcopy(last_info['kalman_filter'])
        STrack.multi_predict(strack_pool)
        dists = matching.embedding_distance(strack_pool, detections)
        dists = matching.fuse_motion(kalman_filter, dists, strack_pool, detections)
        matches, u_track, u_detection = matching.linear_assignment(dists, thresh=0.7)

        for itracked, idet in matches:
            track = strack_pool[itracked]
            det = detections[idet]
            if idet == ae_attack_ind:
                ae_attack_id = track.track_id
            elif idet == ae_target_ind:
                ae_target_id = track.track_id

        # if ae_attack_id is not None and ae_target_id is not None:
        #     return id_features_, output, ae_attack_id, ae_target_id

        ''' Step 3: Second association, with IOU'''
        for i, idet in enumerate(u_detection):
            if idet == ae_attack_ind:
                ae_attack_ind = i
            elif idet == ae_target_ind:
                ae_target_ind = i
        detections = [detections[i] for i in u_detection]
        r_tracked_stracks = [strack_pool[i] for i in u_track if strack_pool[i].state == TrackState.Tracked]
        dists = matching.iou_distance(r_tracked_stracks, detections)
        matches, u_track, u_detection = matching.linear_assignment(dists, thresh=0.5)

        for itracked, idet in matches:
            track = r_tracked_stracks[itracked]
            det = detections[idet]
            if idet == ae_attack_ind:
                ae_attack_id = track.track_id
            elif idet == ae_target_ind:
                ae_target_id = track.track_id

        # if ae_attack_id is not None and ae_target_id is not None:
        #     return id_features_, output, ae_attack_id, ae_target_id

        '''Deal with unconfirmed tracks, usually tracks with only one beginning frame'''
        for i, idet in enumerate(u_detection):
            if idet == ae_attack_ind:
                ae_attack_ind = i
            elif idet == ae_target_ind:
                ae_target_ind = i
        detections = [detections[i] for i in u_detection]
        dists = matching.iou_distance(unconfirmed, detections)
        matches, u_unconfirmed, u_detection = matching.linear_assignment(dists, thresh=0.7)
        for itracked, idet in matches:
            track = unconfirmed[itracked]
            if idet == ae_attack_ind:
                ae_attack_id = track.track_id
            elif idet == ae_target_ind:
                ae_target_id = track.track_id

        return id_features_, output, ae_attack_id, ae_target_id, hm_index

    def forwardFeatureMt(self, im_blob, img0, dets_, inds_, remain_inds_, attack_ids, attack_inds, target_ids,
                         target_inds, last_info, grad=True):
        width = img0.shape[1]
        height = img0.shape[0]
        inp_height = im_blob.shape[2]
        inp_width = im_blob.shape[3]
        c = np.array([width / 2., height / 2.], dtype=np.float32)
        s = max(float(inp_width) / float(inp_height) * height, width) * 1.0
        meta = {'c': c, 's': s,
                'out_height': inp_height // self.opt.down_ratio,
                'out_width': inp_width // self.opt.down_ratio}

        im_blob.requires_grad = True
        self.model.zero_grad()
        if grad:
            output = self.model(im_blob)[-1]
        else:
            with torch.no_grad():
                output = self.model(im_blob)[-1]
        hm = output['hm'].sigmoid()
        wh = output['wh']
        id_feature = output['id']
        id_feature = F.normalize(id_feature, dim=1)

        reg = output['reg'] if self.opt.reg_offset else None
        dets_raw, inds = mot_decode(hm, wh, reg=reg, cat_spec_wh=self.opt.cat_spec_wh, K=self.opt.K)

        dets = self.post_process(dets_raw.clone(), meta)
        dets = self.merge_outputs([dets])[1]
        dets_index = [i for i in range(len(dets))]

        remain_inds = dets[:, 4] > self.opt.conf_thres
        dets = dets[remain_inds]

        ious = bbox_ious(np.ascontiguousarray(dets_[:, :4], dtype=np.float64),
                         np.ascontiguousarray(dets[:, :4], dtype=np.float64))

        row_inds, col_inds = linear_sum_assignment(-ious)

        match = True

        if target_inds is not None:
            for index, attack_ind in enumerate(attack_inds):
                target_ind = target_inds[index]
                if attack_ind not in row_inds or target_ind not in row_inds:
                    match = False
                    break
                att_index = row_inds.tolist().index(attack_ind)
                tar_index = row_inds.tolist().index(target_ind)
                if ious[attack_ind, col_inds[att_index]] < 0.6 or ious[target_ind, col_inds[tar_index]] < 0.6:
                    match = False
                    break
        else:
            for index, attack_ind in enumerate(attack_inds):
                if attack_ind not in row_inds:
                    match = False
                    break
                att_index = row_inds.tolist().index(attack_ind)
                if ious[attack_ind, col_inds[att_index]] < 0.8:
                    match = False
                    break

        if not match:
            dets = dets_
            inds = inds_
            remain_inds = remain_inds_
        # assert match
        id_features = []
        for i in range(3):
            for j in range(3):
                id_feature_exp = _tranpose_and_gather_feat_expand(id_feature, inds, bias=(i - 1, j - 1)).squeeze(0)
                id_features.append(id_feature_exp)

        for i in range(len(id_features)):
            id_features[i] = id_features[i][remain_inds]

        fail_ids = 0

        if not match:
            return id_features, output, None

        ae_attack_inds = []
        ae_attack_ids = []
        for i in range(len(row_inds)):
            if ious[row_inds[i], col_inds[i]] > 0.6:
                if row_inds[i] in attack_inds:
                    ae_attack_inds.append(col_inds[i])
                    index = attack_inds.tolist().index(row_inds[i])
                    ae_attack_ids.append(self.multiple_ori2att[attack_ids[index]])

        # ae_attack_inds = [col_inds[row_inds == attack_ind] for attack_ind in attack_inds]

        # ae_attack_inds = np.concatenate(ae_attack_inds)

        id_features_ = [torch.zeros([len(dets_), id_features[0].size(1)]).to(id_features[0].device) for _ in range(len(id_features))]
        for i in range(9):
            id_features_[i][row_inds] = id_features[i][col_inds]

        id_feature = _tranpose_and_gather_feat_expand(id_feature, inds)
        id_feature = id_feature.squeeze(0)
        id_feature = id_feature[remain_inds]
        id_feature = id_feature.detach().cpu().numpy()

        if len(dets) > 0:
            '''Detections'''
            detections = [STrack(STrack.tlbr_to_tlwh(tlbrs[:4]), tlbrs[4], f, 30) for
                          (tlbrs, f) in zip(dets[:, :5], id_feature)]
        else:
            detections = []

        unconfirmed = copy.deepcopy(last_info['last_unconfirmed'])
        strack_pool = copy.deepcopy(last_info['last_strack_pool'])
        kalman_filter = copy.deepcopy(last_info['kalman_filter'])
        STrack.multi_predict(strack_pool)
        dists = matching.embedding_distance(strack_pool, detections)
        dists = matching.fuse_motion(kalman_filter, dists, strack_pool, detections)
        matches, u_track, u_detection = matching.linear_assignment(dists, thresh=0.7)

        for itracked, idet in matches:
            track = strack_pool[itracked]
            det = detections[idet]
            if dets_index[idet] in ae_attack_inds:
                index = ae_attack_inds.index(dets_index[idet])
                if track.track_id == ae_attack_ids[index]:
                    fail_ids += 1

        ''' Step 3: Second association, with IOU'''
        dets_index = [dets_index[i] for i in u_detection]
        detections = [detections[i] for i in u_detection]
        r_tracked_stracks = [strack_pool[i] for i in u_track if strack_pool[i].state == TrackState.Tracked]
        dists = matching.iou_distance(r_tracked_stracks, detections)
        matches, u_track, u_detection = matching.linear_assignment(dists, thresh=0.5)

        for itracked, idet in matches:
            track = r_tracked_stracks[itracked]
            det = detections[idet]
            if dets_index[idet] in ae_attack_inds:
                index = ae_attack_inds.index(dets_index[idet])
                if track.track_id == ae_attack_ids[index]:
                    fail_ids += 1

        '''Deal with unconfirmed tracks, usually tracks with only one beginning frame'''
        dets_index = [dets_index[i] for i in u_detection]
        detections = [detections[i] for i in u_detection]
        dists = matching.iou_distance(unconfirmed, detections)
        matches, u_unconfirmed, u_detection = matching.linear_assignment(dists, thresh=0.7)
        for itracked, idet in matches:
            track = unconfirmed[itracked]
            if dets_index[idet] in ae_attack_inds:
                index = ae_attack_inds.index(dets_index[idet])
                if track.track_id == ae_attack_ids[index]:
                    fail_ids += 1

        return id_features_, output, fail_ids

    def CheckFit(self, dets, id_feature, attack_ids, attack_inds):
        ad_attack_ids_ = [self.multiple_ori2att[attack_id] for attack_id in attack_ids] \
            if self.opt.attack == 'multiple' else attack_ids
        attack_dets = dets[attack_inds, :4]
        ad_attack_dets = []
        ad_attack_ids = []
        if len(dets) > 0:
            '''Detections'''
            detections = [STrack(STrack.tlbr_to_tlwh(tlbrs[:4]), tlbrs[4], f, 30) for
                          (tlbrs, f) in zip(dets[:, :5], id_feature)]
        else:
            detections = []

        unconfirmed = copy.deepcopy(self.ad_last_info['last_unconfirmed'])
        strack_pool = copy.deepcopy(self.ad_last_info['last_strack_pool'])
        kalman_filter = copy.deepcopy(self.ad_last_info['kalman_filter'])

        STrack.multi_predict(strack_pool)
        dists = matching.embedding_distance(strack_pool, detections)
        dists = matching.fuse_motion(kalman_filter, dists, strack_pool, detections)
        matches, u_track, u_detection = matching.linear_assignment(dists, thresh=0.7)

        for itracked, idet in matches:
            track = strack_pool[itracked]
            det = detections[idet]
            if track.track_id in ad_attack_ids_:
                ad_attack_dets.append(det.tlbr)
                ad_attack_ids.append(track.track_id)

        ''' Step 3: Second association, with IOU'''
        detections = [detections[i] for i in u_detection]
        r_tracked_stracks = [strack_pool[i] for i in u_track if strack_pool[i].state == TrackState.Tracked]
        dists = matching.iou_distance(r_tracked_stracks, detections)
        matches, u_track, u_detection = matching.linear_assignment(dists, thresh=0.5)

        for itracked, idet in matches:
            track = r_tracked_stracks[itracked]
            det = detections[idet]
            if track.track_id in ad_attack_ids_:
                ad_attack_dets.append(det.tlbr)
                ad_attack_ids.append(track.track_id)

        '''Deal with unconfirmed tracks, usually tracks with only one beginning frame'''
        detections = [detections[i] for i in u_detection]
        dists = matching.iou_distance(unconfirmed, detections)
        matches, u_unconfirmed, u_detection = matching.linear_assignment(dists, thresh=0.7)
        for itracked, idet in matches:
            track = unconfirmed[itracked]
            det = detections[idet]
            if track.track_id in ad_attack_ids_:
                ad_attack_dets.append(det.tlbr)
                ad_attack_ids.append(track.track_id)

        if len(ad_attack_dets) == 0:
            return []

        ori_dets = np.array(attack_dets)
        ad_dets = np.array(ad_attack_dets)

        ious = bbox_ious(ori_dets.astype(np.float64), ad_dets.astype(np.float64))
        row_ind, col_ind = linear_sum_assignment(-ious)

        attack_index = []
        for i in range(len(row_ind)):
            if self.opt.attack == 'multiple':
                if ious[row_ind[i], col_ind[i]] > 0.9 and self.multiple_ori2att[attack_ids[row_ind[i]]] == ad_attack_ids[col_ind[i]]:
                    attack_index.append(row_ind[i])
            else:
                if ious[row_ind[i], col_ind[i]] > 0.9:
                    attack_index.append(row_ind[i])

        return attack_index

    def update_attack_sg(self, im_blob, img0, **kwargs):
        self.frame_id_ += 1
        attack_id = kwargs['attack_id']
        self_track_id_ori = kwargs.get('track_id', {}).get('origin', None)
        self_track_id_att = kwargs.get('track_id', {}).get('attack', None)
        activated_starcks = []
        refind_stracks = []
        lost_stracks = []
        removed_stracks = []

        width = img0.shape[1]
        height = img0.shape[0]
        inp_height = im_blob.shape[2]
        inp_width = im_blob.shape[3]
        c = np.array([width / 2., height / 2.], dtype=np.float32)
        s = max(float(inp_width) / float(inp_height) * height, width) * 1.0
        meta = {'c': c, 's': s,
                'out_height': inp_height // self.opt.down_ratio,
                'out_width': inp_width // self.opt.down_ratio}

        ''' Step 1: Network forward, get detections & embeddings'''
        # with torch.no_grad():
        im_blob.requires_grad = True
        self.model.zero_grad()
        output = self.model(im_blob)[-1]
        hm = output['hm'].sigmoid()
        wh = output['wh']
        id_feature = output['id']
        id_feature = F.normalize(id_feature, dim=1)

        reg = output['reg'] if self.opt.reg_offset else None
        dets_raw, inds = mot_decode(hm, wh, reg=reg, cat_spec_wh=self.opt.cat_spec_wh, K=self.opt.K)

        id_features = []
        for i in range(3):
            for j in range(3):
                id_feature_exp = _tranpose_and_gather_feat_expand(id_feature, inds, bias=(i - 1, j - 1)).squeeze(0)
                id_features.append(id_feature_exp)

        id_feature = _tranpose_and_gather_feat_expand(id_feature, inds)

        id_feature = id_feature.squeeze(0)

        dets = self.post_process(dets_raw.clone(), meta)
        dets = self.merge_outputs([dets])[1]

        remain_inds = dets[:, 4] > self.opt.conf_thres
        dets = dets[remain_inds]
        id_feature = id_feature[remain_inds]

        for i in range(len(id_features)):
            id_features[i] = id_features[i][remain_inds]

        id_feature = id_feature.detach().cpu().numpy()

        last_id_features = [None for _ in range(len(dets))]
        last_ad_id_features = [None for _ in range(len(dets))]
        dets_index = [i for i in range(len(dets))]
        dets_ids = [None for _ in range(len(dets))]
        tracks_ad = []

        # import pdb; pdb.set_trace()
        # vis
        '''
        for i in range(0, dets.shape[0]):
            bbox = dets[i][0:4]
            cv2.rectangle(img0, (bbox[0], bbox[1]),
                          (bbox[2], bbox[3]),
                          (0, 255, 0), 2)
        cv2.imshow('dets', img0)
        cv2.waitKey(0)
        id0 = id0-1
        '''

        if len(dets) > 0:
            '''Detections'''
            detections = [STrack(STrack.tlbr_to_tlwh(tlbrs[:4]), tlbrs[4], f, 30) for
                          (tlbrs, f) in zip(dets[:, :5], id_feature)]
        else:
            detections = []

        ''' Add newly detected tracklets to tracked_stracks'''
        unconfirmed = []
        tracked_stracks = []  # type: list[STrack]
        for track in self.tracked_stracks_:
            if not track.is_activated:
                unconfirmed.append(track)
            else:
                tracked_stracks.append(track)

        ''' Step 2: First association, with embedding'''
        strack_pool = joint_stracks(tracked_stracks, self.lost_stracks_)

        STrack.multi_predict(strack_pool)
        dists = matching.embedding_distance(strack_pool, detections)
        # dists = matching.gate_cost_matrix(self.kalman_filter, dists, strack_pool, detections)
        dists = matching.fuse_motion(self.kalman_filter_, dists, strack_pool, detections)
        matches, u_track, u_detection = matching.linear_assignment(dists, thresh=0.7)
        # import pdb; pdb.set_trace()
        for itracked, idet in matches:
            track = strack_pool[itracked]
            det = detections[idet]
            assert last_id_features[dets_index[idet]] is None
            assert last_ad_id_features[dets_index[idet]] is None
            last_id_features[dets_index[idet]] = track.smooth_feat
            last_ad_id_features[dets_index[idet]] = track.smooth_feat_ad
            tracks_ad.append((track, dets_index[idet]))
            if track.state == TrackState.Tracked:
                track.update(detections[idet], self.frame_id_)
                activated_starcks.append(track)
            else:
                track.re_activate_(det, self.frame_id_, new_id=False)
                refind_stracks.append(track)
            dets_ids[dets_index[idet]] = track.track_id

        ''' Step 3: Second association, with IOU'''
        dets_index = [dets_index[i] for i in u_detection]
        detections = [detections[i] for i in u_detection]
        r_tracked_stracks = [strack_pool[i] for i in u_track if strack_pool[i].state == TrackState.Tracked]
        dists = matching.iou_distance(r_tracked_stracks, detections)
        matches, u_track, u_detection = matching.linear_assignment(dists, thresh=0.5)

        for itracked, idet in matches:
            track = r_tracked_stracks[itracked]
            det = detections[idet]
            assert last_id_features[dets_index[idet]] is None
            assert last_ad_id_features[dets_index[idet]] is None
            last_id_features[dets_index[idet]] = track.smooth_feat
            last_ad_id_features[dets_index[idet]] = track.smooth_feat_ad
            tracks_ad.append((track, dets_index[idet]))
            if track.state == TrackState.Tracked:
                track.update(det, self.frame_id_)
                activated_starcks.append(track)
            else:
                track.re_activate_(det, self.frame_id_, new_id=False)
                refind_stracks.append(track)
            dets_ids[dets_index[idet]] = track.track_id

        for it in u_track:
            track = r_tracked_stracks[it]
            if not track.state == TrackState.Lost:
                track.mark_lost()
                lost_stracks.append(track)

        '''Deal with unconfirmed tracks, usually tracks with only one beginning frame'''
        dets_index = [dets_index[i] for i in u_detection]
        detections = [detections[i] for i in u_detection]
        dists = matching.iou_distance(unconfirmed, detections)
        matches, u_unconfirmed, u_detection = matching.linear_assignment(dists, thresh=0.7)
        for itracked, idet in matches:
            assert last_id_features[dets_index[idet]] is None
            assert last_ad_id_features[dets_index[idet]] is None
            last_id_features[dets_index[idet]] = unconfirmed[itracked].smooth_feat
            last_ad_id_features[dets_index[idet]] = unconfirmed[itracked].smooth_feat_ad
            tracks_ad.append((unconfirmed[itracked], dets_index[idet]))
            unconfirmed[itracked].update(detections[idet], self.frame_id_)
            activated_starcks.append(unconfirmed[itracked])
            dets_ids[dets_index[idet]] = unconfirmed[itracked].track_id
        for it in u_unconfirmed:
            track = unconfirmed[it]
            track.mark_removed()
            removed_stracks.append(track)

        """ Step 4: Init new stracks"""
        for inew in u_detection:
            track = detections[inew]
            if track.score < self.det_thresh:
                continue
            track.activate_(self.kalman_filter_, self.frame_id_, track_id=self_track_id_ori)
            activated_starcks.append(track)
            dets_ids[dets_index[inew]] = track.track_id
        """ Step 5: Update state"""
        for track in self.lost_stracks_:
            if self.frame_id_ - track.end_frame > self.max_time_lost:
                track.mark_removed()
                removed_stracks.append(track)

        # print('Ramained match {} s'.format(t4-t3))

        self.tracked_stracks_ = [t for t in self.tracked_stracks_ if t.state == TrackState.Tracked]
        self.tracked_stracks_ = joint_stracks(self.tracked_stracks_, activated_starcks)
        self.tracked_stracks_ = joint_stracks(self.tracked_stracks_, refind_stracks)
        self.lost_stracks_ = sub_stracks(self.lost_stracks_, self.tracked_stracks_)
        self.lost_stracks_.extend(lost_stracks)
        self.lost_stracks_ = sub_stracks(self.lost_stracks_, self.removed_stracks_)
        self.removed_stracks_.extend(removed_stracks)
        self.tracked_stracks_, self.lost_stracks_ = remove_duplicate_stracks(self.tracked_stracks_, self.lost_stracks_)
        # get scores of lost tracks
        output_stracks_ori = [track for track in self.tracked_stracks_ if track.is_activated]

        logger.debug('===========Frame {}=========='.format(self.frame_id_))
        logger.debug('Activated: {}'.format([track.track_id for track in activated_starcks]))
        logger.debug('Refind: {}'.format([track.track_id for track in refind_stracks]))
        logger.debug('Lost: {}'.format([track.track_id for track in lost_stracks]))
        logger.debug('Removed: {}'.format([track.track_id for track in removed_stracks]))

        noise = None
        suc = 0
        for attack_ind, track_id in enumerate(dets_ids):
            if track_id == attack_id:
                if self.opt.attack_id > 0:
                    if not hasattr(self, f'frames_{attack_id}'):
                        setattr(self, f'frames_{attack_id}', 0)
                    if getattr(self, f'frames_{attack_id}') < self.FRAME_THR:
                        setattr(self, f'frames_{attack_id}', getattr(self, f'frames_{attack_id}') + 1)
                        break
                fit = self.CheckFit(dets, id_feature, [attack_id], [attack_ind])
                ious = bbox_ious(np.ascontiguousarray(dets[:, :4], dtype=np.float64),
                                 np.ascontiguousarray(dets[:, :4], dtype=np.float64))

                ious[range(len(dets)), range(len(dets))] = 0
                dis = bbox_dis(np.ascontiguousarray(dets[:, :4], dtype=np.float64),
                               np.ascontiguousarray(dets[:, :4], dtype=np.float64))
                dis[range(len(dets)), range(len(dets))] = np.inf
                target_ind = np.argmax(ious[attack_ind])
                if ious[attack_ind][target_ind] >= self.attack_iou_thr:
                    if ious[attack_ind][target_ind] == 0:
                        target_ind = np.argmin(dis[attack_ind])
                    target_id = dets_ids[target_ind]
                    if fit:
                        if self.opt.rand:
                            noise, attack_iter, suc = self.attack_sg_random(
                                im_blob,
                                img0,
                                id_features,
                                dets,
                                inds,
                                remain_inds,
                                last_info=self.ad_last_info,
                                outputs_ori=output,
                                attack_id=attack_id,
                                attack_ind=attack_ind,
                                target_id=target_id,
                                target_ind=target_ind
                            )
                        else:
                            noise, attack_iter, suc = self.attack_sg(
                                im_blob,
                                img0,
                                id_features,
                                dets,
                                inds,
                                remain_inds,
                                last_info=self.ad_last_info,
                                outputs_ori=output,
                                attack_id=attack_id,
                                attack_ind=attack_ind,
                                target_id=target_id,
                                target_ind=target_ind
                            )
                        self.attack_iou_thr = 0
                        if suc:
                            suc = 1
                            print(
                                f'attack id: {attack_id}\tattack frame {self.frame_id_}: SUCCESS\tl2 distance: {(noise ** 2).sum().sqrt().item()}\titeration: {attack_iter}')
                        else:
                            suc = 2
                            print(
                                f'attack id: {attack_id}\tattack frame {self.frame_id_}: FAIL\tl2 distance: {(noise ** 2).sum().sqrt().item() if noise is not None else None}\titeration: {attack_iter}')
                    else:
                        suc = 3
                    if ious[attack_ind][target_ind] == 0:
                        self.temp_i += 1
                        if self.temp_i >= 10:
                            self.attack_iou_thr = self.ATTACK_IOU_THR
                    else:
                        self.temp_i = 0
                else:
                    self.attack_iou_thr = self.ATTACK_IOU_THR
                    if fit:
                        suc = 2

        if noise is not None:
            l2_dis = (noise ** 2).sum().sqrt().item()
            adImg = torch.clip(im_blob + noise, min=0, max=1)

            noise = self.recoverNoise(noise, img0)
            # adImg = np.clip(img0 + noise, a_min=0, a_max=255)

            # noise = adImg - img0
            noise = (noise - np.min(noise)) / (np.max(noise) - np.min(noise))
            noise = (noise * 255).astype(np.uint8)
        else:
            l2_dis = None
            adImg = im_blob
        output_stracks_att = self.update(adImg, img0, track_id=self_track_id_att)
        adImg = self.recoverNoise(adImg.detach(), img0)
        return output_stracks_ori, output_stracks_att, adImg, noise, l2_dis, suc

    def update_attack_mt(self, im_blob, img0, **kwargs):
        self.frame_id_ += 1
        activated_starcks = []
        refind_stracks = []
        lost_stracks = []
        removed_stracks = []

        width = img0.shape[1]
        height = img0.shape[0]
        inp_height = im_blob.shape[2]
        inp_width = im_blob.shape[3]
        c = np.array([width / 2., height / 2.], dtype=np.float32)
        s = max(float(inp_width) / float(inp_height) * height, width) * 1.0
        meta = {'c': c, 's': s,
                'out_height': inp_height // self.opt.down_ratio,
                'out_width': inp_width // self.opt.down_ratio}

        ''' Step 1: Network forward, get detections & embeddings'''
        # with torch.no_grad():
        im_blob.requires_grad = True
        self.model.zero_grad()
        output = self.model(im_blob)[-1]
        hm = output['hm'].sigmoid()
        wh = output['wh']
        id_feature = output['id']
        id_feature = F.normalize(id_feature, dim=1)

        reg = output['reg'] if self.opt.reg_offset else None
        dets_raw, inds = mot_decode(hm, wh, reg=reg, cat_spec_wh=self.opt.cat_spec_wh, K=self.opt.K)

        id_features = []
        for i in range(3):
            for j in range(3):
                id_feature_exp = _tranpose_and_gather_feat_expand(id_feature, inds, bias=(i - 1, j - 1)).squeeze(0)
                id_features.append(id_feature_exp)

        id_feature = _tranpose_and_gather_feat_expand(id_feature, inds)

        id_feature = id_feature.squeeze(0)

        dets = self.post_process(dets_raw.clone(), meta)
        dets = self.merge_outputs([dets])[1]

        remain_inds = dets[:, 4] > self.opt.conf_thres
        dets = dets[remain_inds]
        id_feature = id_feature[remain_inds]

        for i in range(len(id_features)):
            id_features[i] = id_features[i][remain_inds]

        id_feature = id_feature.detach().cpu().numpy()

        last_id_features = [None for _ in range(len(dets))]
        last_ad_id_features = [None for _ in range(len(dets))]
        dets_index = [i for i in range(len(dets))]
        dets_ids = [None for _ in range(len(dets))]
        tracks_ad = []

        # import pdb; pdb.set_trace()
        # vis
        '''
        for i in range(0, dets.shape[0]):
            bbox = dets[i][0:4]
            cv2.rectangle(img0, (bbox[0], bbox[1]),
                          (bbox[2], bbox[3]),
                          (0, 255, 0), 2)
        cv2.imshow('dets', img0)
        cv2.waitKey(0)
        id0 = id0-1
        '''

        if len(dets) > 0:
            '''Detections'''
            detections = [STrack(STrack.tlbr_to_tlwh(tlbrs[:4]), tlbrs[4], f, 30) for
                          (tlbrs, f) in zip(dets[:, :5], id_feature)]
        else:
            detections = []

        ''' Add newly detected tracklets to tracked_stracks'''
        unconfirmed = []
        tracked_stracks = []  # type: list[STrack]
        for track in self.tracked_stracks_:
            if not track.is_activated:
                unconfirmed.append(track)
            else:
                tracked_stracks.append(track)

        ''' Step 2: First association, with embedding'''
        strack_pool = joint_stracks(tracked_stracks, self.lost_stracks_)

        STrack.multi_predict(strack_pool)
        dists = matching.embedding_distance(strack_pool, detections)
        dists = matching.fuse_motion(self.kalman_filter_, dists, strack_pool, detections)
        matches, u_track, u_detection = matching.linear_assignment(dists, thresh=0.7)
        # import pdb; pdb.set_trace()
        for itracked, idet in matches:
            track = strack_pool[itracked]
            det = detections[idet]
            assert last_id_features[dets_index[idet]] is None
            assert last_ad_id_features[dets_index[idet]] is None
            last_id_features[dets_index[idet]] = track.smooth_feat
            last_ad_id_features[dets_index[idet]] = track.smooth_feat_ad
            tracks_ad.append((track, dets_index[idet]))
            if track.state == TrackState.Tracked:
                track.update(detections[idet], self.frame_id_)
                activated_starcks.append(track)
            else:
                track.re_activate_(det, self.frame_id_, new_id=False)
                refind_stracks.append(track)
            dets_ids[dets_index[idet]] = track.track_id

        ''' Step 3: Second association, with IOU'''
        dets_index = [dets_index[i] for i in u_detection]
        detections = [detections[i] for i in u_detection]
        r_tracked_stracks = [strack_pool[i] for i in u_track if strack_pool[i].state == TrackState.Tracked]
        dists = matching.iou_distance(r_tracked_stracks, detections)
        matches, u_track, u_detection = matching.linear_assignment(dists, thresh=0.5)

        for itracked, idet in matches:
            track = r_tracked_stracks[itracked]
            det = detections[idet]
            assert last_id_features[dets_index[idet]] is None
            assert last_ad_id_features[dets_index[idet]] is None
            last_id_features[dets_index[idet]] = track.smooth_feat
            last_ad_id_features[dets_index[idet]] = track.smooth_feat_ad
            tracks_ad.append((track, dets_index[idet]))
            if track.state == TrackState.Tracked:
                track.update(det, self.frame_id_)
                activated_starcks.append(track)
            else:
                track.re_activate_(det, self.frame_id_, new_id=False)
                refind_stracks.append(track)
            dets_ids[dets_index[idet]] = track.track_id

        for it in u_track:
            track = r_tracked_stracks[it]
            if not track.state == TrackState.Lost:
                track.mark_lost()
                lost_stracks.append(track)

        '''Deal with unconfirmed tracks, usually tracks with only one beginning frame'''
        dets_index = [dets_index[i] for i in u_detection]
        detections = [detections[i] for i in u_detection]
        dists = matching.iou_distance(unconfirmed, detections)
        matches, u_unconfirmed, u_detection = matching.linear_assignment(dists, thresh=0.7)
        for itracked, idet in matches:
            assert last_id_features[dets_index[idet]] is None
            assert last_ad_id_features[dets_index[idet]] is None
            last_id_features[dets_index[idet]] = unconfirmed[itracked].smooth_feat
            last_ad_id_features[dets_index[idet]] = unconfirmed[itracked].smooth_feat_ad
            tracks_ad.append((unconfirmed[itracked], dets_index[idet]))
            unconfirmed[itracked].update(detections[idet], self.frame_id_)
            activated_starcks.append(unconfirmed[itracked])
            dets_ids[dets_index[idet]] = unconfirmed[itracked].track_id

        for it in u_unconfirmed:
            track = unconfirmed[it]
            track.mark_removed()
            removed_stracks.append(track)

        """ Step 4: Init new stracks"""
        for inew in u_detection:
            track = detections[inew]
            if track.score < self.det_thresh:
                continue
            track.activate_(self.kalman_filter_, self.frame_id_)
            activated_starcks.append(track)
            dets_ids[dets_index[inew]] = track.track_id

        """ Step 5: Update state"""
        for track in self.lost_stracks_:
            if self.frame_id_ - track.end_frame > self.max_time_lost:
                track.mark_removed()
                removed_stracks.append(track)

        # print('Ramained match {} s'.format(t4-t3))

        self.tracked_stracks_ = [t for t in self.tracked_stracks_ if t.state == TrackState.Tracked]
        self.tracked_stracks_ = joint_stracks(self.tracked_stracks_, activated_starcks)
        self.tracked_stracks_ = joint_stracks(self.tracked_stracks_, refind_stracks)
        self.lost_stracks_ = sub_stracks(self.lost_stracks_, self.tracked_stracks_)
        self.lost_stracks_.extend(lost_stracks)
        self.lost_stracks_ = sub_stracks(self.lost_stracks_, self.removed_stracks_)
        self.removed_stracks_.extend(removed_stracks)
        self.tracked_stracks_, self.lost_stracks_ = remove_duplicate_stracks(self.tracked_stracks_, self.lost_stracks_)
        # get scores of lost tracks
        output_stracks_ori = [track for track in self.tracked_stracks_ if track.is_activated]
        id_set = set([track.track_id for track in output_stracks_ori])
        for i in range(len(dets_ids)):
            if dets_ids[i] is not None and dets_ids[i] not in id_set:
                dets_ids[i] = None

        output_stracks_ori_ind = []
        for ind, track in enumerate(output_stracks_ori):
            if track.track_id not in self.multiple_ori_ids:
                self.multiple_ori_ids[track.track_id] = 0
            self.multiple_ori_ids[track.track_id] += 1
            if self.multiple_ori_ids[track.track_id] <= self.FRAME_THR:
                output_stracks_ori_ind.append(ind)

        logger.debug('===========Frame {}=========='.format(self.frame_id_))
        logger.debug('Activated: {}'.format([track.track_id for track in activated_starcks]))
        logger.debug('Refind: {}'.format([track.track_id for track in refind_stracks]))
        logger.debug('Lost: {}'.format([track.track_id for track in lost_stracks]))
        logger.debug('Removed: {}'.format([track.track_id for track in removed_stracks]))

        attack_ids = []
        target_ids = []
        attack_inds = []
        target_inds = []

        noise = None
        if len(dets) > 0:
            ious = bbox_ious(np.ascontiguousarray(dets[:, :4], dtype=np.float64),
                             np.ascontiguousarray(dets[:, :4], dtype=np.float64))
            ious[range(len(dets)), range(len(dets))] = 0
            ious_inds = np.argmax(ious, axis=1)
            dis = bbox_dis(np.ascontiguousarray(dets[:, :4], dtype=np.float64),
                           np.ascontiguousarray(dets[:, :4], dtype=np.float64))
            dis[range(len(dets)), range(len(dets))] = np.inf
            dis_inds = np.argmin(dis, axis=1)
            for attack_ind, track_id in enumerate(dets_ids):
                if track_id is None or self.multiple_ori_ids[track_id] <= self.FRAME_THR \
                        or dets_ids[ious_inds[attack_ind]] not in self.multiple_ori2att \
                        or track_id not in self.multiple_ori2att:
                    continue
                if ious[attack_ind, ious_inds[attack_ind]] > self.ATTACK_IOU_THR or (
                        track_id in self.low_iou_ids and ious[attack_ind, ious_inds[attack_ind]] > 0
                ):
                    attack_ids.append(track_id)
                    target_ids.append(dets_ids[ious_inds[attack_ind]])
                    attack_inds.append(attack_ind)
                    target_inds.append(ious_inds[attack_ind])
                    if hasattr(self, f'temp_i_{track_id}'):
                        self.__setattr__(f'temp_i_{track_id}', 0)
                elif ious[attack_ind, ious_inds[attack_ind]] == 0 and track_id in self.low_iou_ids:
                    if hasattr(self, f'temp_i_{track_id}'):
                        self.__setattr__(f'temp_i_{track_id}', self.__getattribute__(f'temp_i_{track_id}') + 1)
                    else:
                        self.__setattr__(f'temp_i_{track_id}', 1)
                    if self.__getattribute__(f'temp_i_{track_id}') > 10:
                        self.low_iou_ids.remove(track_id)
                    elif dets_ids[dis_inds[attack_ind]] in self.multiple_ori2att:
                        attack_ids.append(track_id)
                        target_ids.append(dets_ids[dis_inds[attack_ind]])
                        attack_inds.append(attack_ind)
                        target_inds.append(dis_inds[attack_ind])
            fit_index = self.CheckFit(dets, id_feature, attack_ids, attack_inds) if len(attack_ids) else []
            if fit_index:
                attack_ids = np.array(attack_ids)[fit_index]
                target_ids = np.array(target_ids)[fit_index]
                attack_inds = np.array(attack_inds)[fit_index]
                target_inds = np.array(target_inds)[fit_index]

                if self.opt.rand:
                    noise, attack_iter, suc = self.attack_mt_random(
                        im_blob,
                        img0,
                        id_features,
                        dets,
                        inds,
                        remain_inds,
                        last_info=self.ad_last_info,
                        outputs_ori=output,
                        attack_ids=attack_ids,
                        attack_inds=attack_inds,
                        target_ids=target_ids,
                        target_inds=target_inds
                    )
                else:
                    noise, attack_iter, suc = self.attack_mt(
                        im_blob,
                        img0,
                        id_features,
                        dets,
                        inds,
                        remain_inds,
                        last_info=self.ad_last_info,
                        outputs_ori=output,
                        attack_ids=attack_ids,
                        attack_inds=attack_inds,
                        target_ids=target_ids,
                        target_inds=target_inds
                    )
                self.low_iou_ids.update(set(attack_ids))
                if suc:
                    self.attacked_ids.update(set(attack_ids))
                    print(
                        f'attack ids: {attack_ids}\tattack frame {self.frame_id_}: SUCCESS\tl2 distance: {(noise ** 2).sum().sqrt().item()}\titeration: {attack_iter}')
                else:
                    print(f'attack ids: {attack_ids}\tattack frame {self.frame_id_}: FAIL\tl2 distance: {(noise ** 2).sum().sqrt().item() if noise is not None else None}\titeration: {attack_iter}')

        if noise is not None:
            l2_dis = (noise ** 2).sum().sqrt().item()
            adImg = torch.clip(im_blob + noise, min=0, max=1)

            noise = self.recoverNoise(noise, img0)
            noise = (noise - np.min(noise)) / (np.max(noise) - np.min(noise))
            noise = (noise * 255).astype(np.uint8)
        else:
            l2_dis = None
            adImg = im_blob

        output_stracks_att = self.update(adImg, img0)
        adImg = self.recoverNoise(adImg.detach(), img0)

        output_stracks_att_ind = []
        for ind, track in enumerate(output_stracks_att):
            if track.track_id not in self.multiple_att_ids:
                self.multiple_att_ids[track.track_id] = 0
            self.multiple_att_ids[track.track_id] += 1
            if self.multiple_att_ids[track.track_id] <= self.FRAME_THR:
                output_stracks_att_ind.append(ind)
        if len(output_stracks_ori_ind) and len(output_stracks_att_ind):
            ori_dets = [track.curr_tlbr for i, track in enumerate(output_stracks_ori) if i in output_stracks_ori_ind]
            att_dets = [track.curr_tlbr for i, track in enumerate(output_stracks_att) if i in output_stracks_att_ind]
            ori_dets = np.stack(ori_dets).astype(np.float64)
            att_dets = np.stack(att_dets).astype(np.float64)
            ious = bbox_ious(ori_dets, att_dets)
            row_ind, col_ind = linear_sum_assignment(-ious)
            for i in range(len(row_ind)):
                if ious[row_ind[i], col_ind[i]] > 0.9:
                    ori_id = output_stracks_ori[output_stracks_ori_ind[row_ind[i]]].track_id
                    att_id = output_stracks_att[output_stracks_att_ind[col_ind[i]]].track_id
                    self.multiple_ori2att[ori_id] = att_id
        return output_stracks_ori, output_stracks_att, adImg, noise, l2_dis

    def update_attack_sg_feat(self, im_blob, img0, **kwargs):
        self.frame_id_ += 1
        attack_id = kwargs['attack_id']
        self_track_id_ori = kwargs.get('track_id', {}).get('origin', None)
        self_track_id_att = kwargs.get('track_id', {}).get('attack', None)
        activated_starcks = []
        refind_stracks = []
        lost_stracks = []
        removed_stracks = []

        width = img0.shape[1]
        height = img0.shape[0]
        inp_height = im_blob.shape[2]
        inp_width = im_blob.shape[3]
        c = np.array([width / 2., height / 2.], dtype=np.float32)
        s = max(float(inp_width) / float(inp_height) * height, width) * 1.0
        meta = {'c': c, 's': s,
                'out_height': inp_height // self.opt.down_ratio,
                'out_width': inp_width // self.opt.down_ratio}

        ''' Step 1: Network forward, get detections & embeddings'''
        # with torch.no_grad():
        im_blob.requires_grad = True
        self.model.zero_grad()
        output = self.model(im_blob)[-1]
        hm = output['hm'].sigmoid()
        wh = output['wh']
        id_feature = output['id']
        id_feature = F.normalize(id_feature, dim=1)

        reg = output['reg'] if self.opt.reg_offset else None
        dets_raw, inds = mot_decode(hm, wh, reg=reg, cat_spec_wh=self.opt.cat_spec_wh, K=self.opt.K)

        id_features = []
        for i in range(3):
            for j in range(3):
                id_feature_exp = _tranpose_and_gather_feat_expand(id_feature, inds, bias=(i - 1, j - 1)).squeeze(0)
                id_features.append(id_feature_exp)

        id_feature = _tranpose_and_gather_feat_expand(id_feature, inds)

        id_feature = id_feature.squeeze(0)

        dets = self.post_process(dets_raw.clone(), meta)
        dets = self.merge_outputs([dets])[1]

        remain_inds = dets[:, 4] > self.opt.conf_thres
        dets = dets[remain_inds]
        id_feature = id_feature[remain_inds]

        for i in range(len(id_features)):
            id_features[i] = id_features[i][remain_inds]

        id_feature = id_feature.detach().cpu().numpy()

        last_id_features = [None for _ in range(len(dets))]
        last_ad_id_features = [None for _ in range(len(dets))]
        dets_index = [i for i in range(len(dets))]
        dets_ids = [None for _ in range(len(dets))]
        tracks_ad = []

        # import pdb; pdb.set_trace()
        # vis
        '''
        for i in range(0, dets.shape[0]):
            bbox = dets[i][0:4]
            cv2.rectangle(img0, (bbox[0], bbox[1]),
                          (bbox[2], bbox[3]),
                          (0, 255, 0), 2)
        cv2.imshow('dets', img0)
        cv2.waitKey(0)
        id0 = id0-1
        '''

        if len(dets) > 0:
            '''Detections'''
            detections = [STrack(STrack.tlbr_to_tlwh(tlbrs[:4]), tlbrs[4], f, 30) for
                          (tlbrs, f) in zip(dets[:, :5], id_feature)]
        else:
            detections = []

        ''' Add newly detected tracklets to tracked_stracks'''
        unconfirmed = []
        tracked_stracks = []  # type: list[STrack]
        for track in self.tracked_stracks_:
            if not track.is_activated:
                unconfirmed.append(track)
            else:
                tracked_stracks.append(track)

        ''' Step 2: First association, with embedding'''
        strack_pool = joint_stracks(tracked_stracks, self.lost_stracks_)

        STrack.multi_predict(strack_pool)
        dists = matching.embedding_distance(strack_pool, detections)
        # dists = matching.gate_cost_matrix(self.kalman_filter, dists, strack_pool, detections)
        dists = matching.fuse_motion(self.kalman_filter_, dists, strack_pool, detections)
        matches, u_track, u_detection = matching.linear_assignment(dists, thresh=0.7)
        # import pdb; pdb.set_trace()
        for itracked, idet in matches:
            track = strack_pool[itracked]
            det = detections[idet]
            assert last_id_features[dets_index[idet]] is None
            assert last_ad_id_features[dets_index[idet]] is None
            last_id_features[dets_index[idet]] = track.smooth_feat
            last_ad_id_features[dets_index[idet]] = track.smooth_feat_ad
            tracks_ad.append((track, dets_index[idet]))
            if track.state == TrackState.Tracked:
                track.update(detections[idet], self.frame_id_)
                activated_starcks.append(track)
            else:
                track.re_activate_(det, self.frame_id_, new_id=False)
                refind_stracks.append(track)
            dets_ids[dets_index[idet]] = track.track_id

        ''' Step 3: Second association, with IOU'''
        dets_index = [dets_index[i] for i in u_detection]
        detections = [detections[i] for i in u_detection]
        r_tracked_stracks = [strack_pool[i] for i in u_track if strack_pool[i].state == TrackState.Tracked]
        dists = matching.iou_distance(r_tracked_stracks, detections)
        matches, u_track, u_detection = matching.linear_assignment(dists, thresh=0.5)

        for itracked, idet in matches:
            track = r_tracked_stracks[itracked]
            det = detections[idet]
            assert last_id_features[dets_index[idet]] is None
            assert last_ad_id_features[dets_index[idet]] is None
            last_id_features[dets_index[idet]] = track.smooth_feat
            last_ad_id_features[dets_index[idet]] = track.smooth_feat_ad
            tracks_ad.append((track, dets_index[idet]))
            if track.state == TrackState.Tracked:
                track.update(det, self.frame_id_)
                activated_starcks.append(track)
            else:
                track.re_activate_(det, self.frame_id_, new_id=False)
                refind_stracks.append(track)
            dets_ids[dets_index[idet]] = track.track_id

        for it in u_track:
            track = r_tracked_stracks[it]
            if not track.state == TrackState.Lost:
                track.mark_lost()
                lost_stracks.append(track)

        '''Deal with unconfirmed tracks, usually tracks with only one beginning frame'''
        dets_index = [dets_index[i] for i in u_detection]
        detections = [detections[i] for i in u_detection]
        dists = matching.iou_distance(unconfirmed, detections)
        matches, u_unconfirmed, u_detection = matching.linear_assignment(dists, thresh=0.7)
        for itracked, idet in matches:
            assert last_id_features[dets_index[idet]] is None
            assert last_ad_id_features[dets_index[idet]] is None
            last_id_features[dets_index[idet]] = unconfirmed[itracked].smooth_feat
            last_ad_id_features[dets_index[idet]] = unconfirmed[itracked].smooth_feat_ad
            tracks_ad.append((unconfirmed[itracked], dets_index[idet]))
            unconfirmed[itracked].update(detections[idet], self.frame_id_)
            activated_starcks.append(unconfirmed[itracked])
            dets_ids[dets_index[idet]] = unconfirmed[itracked].track_id
        for it in u_unconfirmed:
            track = unconfirmed[it]
            track.mark_removed()
            removed_stracks.append(track)

        """ Step 4: Init new stracks"""
        for inew in u_detection:
            track = detections[inew]
            if track.score < self.det_thresh:
                continue
            track.activate_(self.kalman_filter_, self.frame_id_, track_id=self_track_id_ori)
            activated_starcks.append(track)
            dets_ids[dets_index[inew]] = track.track_id
        """ Step 5: Update state"""
        for track in self.lost_stracks_:
            if self.frame_id_ - track.end_frame > self.max_time_lost:
                track.mark_removed()
                removed_stracks.append(track)

        # print('Ramained match {} s'.format(t4-t3))

        self.tracked_stracks_ = [t for t in self.tracked_stracks_ if t.state == TrackState.Tracked]
        self.tracked_stracks_ = joint_stracks(self.tracked_stracks_, activated_starcks)
        self.tracked_stracks_ = joint_stracks(self.tracked_stracks_, refind_stracks)
        self.lost_stracks_ = sub_stracks(self.lost_stracks_, self.tracked_stracks_)
        self.lost_stracks_.extend(lost_stracks)
        self.lost_stracks_ = sub_stracks(self.lost_stracks_, self.removed_stracks_)
        self.removed_stracks_.extend(removed_stracks)
        self.tracked_stracks_, self.lost_stracks_ = remove_duplicate_stracks(self.tracked_stracks_, self.lost_stracks_)
        # get scores of lost tracks
        output_stracks_ori = [track for track in self.tracked_stracks_ if track.is_activated]

        logger.debug('===========Frame {}=========='.format(self.frame_id_))
        logger.debug('Activated: {}'.format([track.track_id for track in activated_starcks]))
        logger.debug('Refind: {}'.format([track.track_id for track in refind_stracks]))
        logger.debug('Lost: {}'.format([track.track_id for track in lost_stracks]))
        logger.debug('Removed: {}'.format([track.track_id for track in removed_stracks]))

        noise = None
        suc = 0
        for attack_ind, track_id in enumerate(dets_ids):
            if track_id == attack_id:
                if self.opt.attack_id > 0:
                    if not hasattr(self, f'frames_{attack_id}'):
                        setattr(self, f'frames_{attack_id}', 0)
                    if getattr(self, f'frames_{attack_id}') < self.FRAME_THR:
                        setattr(self, f'frames_{attack_id}', getattr(self, f'frames_{attack_id}') + 1)
                        break
                fit = self.CheckFit(dets, id_feature, [attack_id], [attack_ind])
                ious = bbox_ious(np.ascontiguousarray(dets[:, :4], dtype=np.float64),
                                 np.ascontiguousarray(dets[:, :4], dtype=np.float64))

                ious[range(len(dets)), range(len(dets))] = 0
                dis = bbox_dis(np.ascontiguousarray(dets[:, :4], dtype=np.float64),
                               np.ascontiguousarray(dets[:, :4], dtype=np.float64))
                dis[range(len(dets)), range(len(dets))] = np.inf
                target_ind = np.argmax(ious[attack_ind])
                if ious[attack_ind][target_ind] >= self.attack_iou_thr:
                    if ious[attack_ind][target_ind] == 0:
                        target_ind = np.argmin(dis[attack_ind])
                    target_id = dets_ids[target_ind]
                    if fit:
                        noise, attack_iter, suc = self.attack_sg_feat(
                            im_blob,
                            img0,
                            id_features,
                            dets,
                            inds,
                            remain_inds,
                            last_info=self.ad_last_info,
                            outputs_ori=output,
                            attack_id=attack_id,
                            attack_ind=attack_ind,
                            target_id=target_id,
                            target_ind=target_ind
                        )
                        self.attack_iou_thr = 0
                        if suc:
                            suc = 1
                            print(
                                f'attack id: {attack_id}\tattack frame {self.frame_id_}: SUCCESS\tl2 distance: {(noise ** 2).sum().sqrt().item()}\titeration: {attack_iter}')
                        else:
                            suc = 2
                            print(
                                f'attack id: {attack_id}\tattack frame {self.frame_id_}: FAIL\tl2 distance: {(noise ** 2).sum().sqrt().item()}\titeration: {attack_iter}')
                    else:
                        suc = 3
                    if ious[attack_ind][target_ind] == 0:
                        self.temp_i += 1
                        if self.temp_i >= 10:
                            self.attack_iou_thr = self.ATTACK_IOU_THR
                    else:
                        self.temp_i = 0
                else:
                    self.attack_iou_thr = self.ATTACK_IOU_THR
                    if fit:
                        suc = 2

        if noise is not None:
            l2_dis = (noise ** 2).sum().sqrt().item()
            adImg = torch.clip(im_blob + noise, min=0, max=1)

            noise = self.recoverNoise(noise, img0)
            noise = (noise - np.min(noise)) / (np.max(noise) - np.min(noise))
            noise = (noise * 255).astype(np.uint8)
        else:
            l2_dis = None
            adImg = im_blob
        output_stracks_att = self.update(adImg, img0, track_id=self_track_id_att)
        adImg = self.recoverNoise(adImg.detach(), img0)
        return output_stracks_ori, output_stracks_att, adImg, noise, l2_dis, suc

    def update_attack_sg_cl(self, im_blob, img0, **kwargs):
        self.frame_id_ += 1
        attack_id = kwargs['attack_id']
        self_track_id_ori = kwargs.get('track_id', {}).get('origin', None)
        self_track_id_att = kwargs.get('track_id', {}).get('attack', None)
        activated_starcks = []
        refind_stracks = []
        lost_stracks = []
        removed_stracks = []

        width = img0.shape[1]
        height = img0.shape[0]
        inp_height = im_blob.shape[2]
        inp_width = im_blob.shape[3]
        c = np.array([width / 2., height / 2.], dtype=np.float32)
        s = max(float(inp_width) / float(inp_height) * height, width) * 1.0
        meta = {'c': c, 's': s,
                'out_height': inp_height // self.opt.down_ratio,
                'out_width': inp_width // self.opt.down_ratio}

        ''' Step 1: Network forward, get detections & embeddings'''
        # with torch.no_grad():
        im_blob.requires_grad = True
        self.model.zero_grad()
        output = self.model(im_blob)[-1]
        hm = output['hm'].sigmoid()
        wh = output['wh']
        id_feature = output['id']
        id_feature = F.normalize(id_feature, dim=1)

        reg = output['reg'] if self.opt.reg_offset else None
        dets_raw, inds = mot_decode(hm, wh, reg=reg, cat_spec_wh=self.opt.cat_spec_wh, K=self.opt.K)

        id_features = []
        for i in range(3):
            for j in range(3):
                id_feature_exp = _tranpose_and_gather_feat_expand(id_feature, inds, bias=(i - 1, j - 1)).squeeze(0)
                id_features.append(id_feature_exp)

        id_feature = _tranpose_and_gather_feat_expand(id_feature, inds)

        id_feature = id_feature.squeeze(0)

        dets = self.post_process(dets_raw.clone(), meta)
        dets = self.merge_outputs([dets])[1]

        remain_inds = dets[:, 4] > self.opt.conf_thres
        dets = dets[remain_inds]
        id_feature = id_feature[remain_inds]

        for i in range(len(id_features)):
            id_features[i] = id_features[i][remain_inds]

        id_feature = id_feature.detach().cpu().numpy()

        last_id_features = [None for _ in range(len(dets))]
        last_ad_id_features = [None for _ in range(len(dets))]
        dets_index = [i for i in range(len(dets))]
        dets_ids = [None for _ in range(len(dets))]
        tracks_ad = []

        # import pdb; pdb.set_trace()
        # vis
        '''
        for i in range(0, dets.shape[0]):
            bbox = dets[i][0:4]
            cv2.rectangle(img0, (bbox[0], bbox[1]),
                          (bbox[2], bbox[3]),
                          (0, 255, 0), 2)
        cv2.imshow('dets', img0)
        cv2.waitKey(0)
        id0 = id0-1
        '''

        if len(dets) > 0:
            '''Detections'''
            detections = [STrack(STrack.tlbr_to_tlwh(tlbrs[:4]), tlbrs[4], f, 30) for
                          (tlbrs, f) in zip(dets[:, :5], id_feature)]
        else:
            detections = []

        ''' Add newly detected tracklets to tracked_stracks'''
        unconfirmed = []
        tracked_stracks = []  # type: list[STrack]
        for track in self.tracked_stracks_:
            if not track.is_activated:
                unconfirmed.append(track)
            else:
                tracked_stracks.append(track)

        ''' Step 2: First association, with embedding'''
        strack_pool = joint_stracks(tracked_stracks, self.lost_stracks_)

        STrack.multi_predict(strack_pool)
        dists = matching.embedding_distance(strack_pool, detections)
        # dists = matching.gate_cost_matrix(self.kalman_filter, dists, strack_pool, detections)
        dists = matching.fuse_motion(self.kalman_filter_, dists, strack_pool, detections)
        matches, u_track, u_detection = matching.linear_assignment(dists, thresh=0.7)
        # import pdb; pdb.set_trace()
        for itracked, idet in matches:
            track = strack_pool[itracked]
            det = detections[idet]
            assert last_id_features[dets_index[idet]] is None
            assert last_ad_id_features[dets_index[idet]] is None
            last_id_features[dets_index[idet]] = track.smooth_feat
            last_ad_id_features[dets_index[idet]] = track.smooth_feat_ad
            tracks_ad.append((track, dets_index[idet]))
            if track.state == TrackState.Tracked:
                track.update(detections[idet], self.frame_id_)
                activated_starcks.append(track)
            else:
                track.re_activate_(det, self.frame_id_, new_id=False)
                refind_stracks.append(track)
            dets_ids[dets_index[idet]] = track.track_id

        ''' Step 3: Second association, with IOU'''
        dets_index = [dets_index[i] for i in u_detection]
        detections = [detections[i] for i in u_detection]
        r_tracked_stracks = [strack_pool[i] for i in u_track if strack_pool[i].state == TrackState.Tracked]
        dists = matching.iou_distance(r_tracked_stracks, detections)
        matches, u_track, u_detection = matching.linear_assignment(dists, thresh=0.5)

        for itracked, idet in matches:
            track = r_tracked_stracks[itracked]
            det = detections[idet]
            assert last_id_features[dets_index[idet]] is None
            assert last_ad_id_features[dets_index[idet]] is None
            last_id_features[dets_index[idet]] = track.smooth_feat
            last_ad_id_features[dets_index[idet]] = track.smooth_feat_ad
            tracks_ad.append((track, dets_index[idet]))
            if track.state == TrackState.Tracked:
                track.update(det, self.frame_id_)
                activated_starcks.append(track)
            else:
                track.re_activate_(det, self.frame_id_, new_id=False)
                refind_stracks.append(track)
            dets_ids[dets_index[idet]] = track.track_id

        for it in u_track:
            track = r_tracked_stracks[it]
            if not track.state == TrackState.Lost:
                track.mark_lost()
                lost_stracks.append(track)

        '''Deal with unconfirmed tracks, usually tracks with only one beginning frame'''
        dets_index = [dets_index[i] for i in u_detection]
        detections = [detections[i] for i in u_detection]
        dists = matching.iou_distance(unconfirmed, detections)
        matches, u_unconfirmed, u_detection = matching.linear_assignment(dists, thresh=0.7)
        for itracked, idet in matches:
            assert last_id_features[dets_index[idet]] is None
            assert last_ad_id_features[dets_index[idet]] is None
            last_id_features[dets_index[idet]] = unconfirmed[itracked].smooth_feat
            last_ad_id_features[dets_index[idet]] = unconfirmed[itracked].smooth_feat_ad
            tracks_ad.append((unconfirmed[itracked], dets_index[idet]))
            unconfirmed[itracked].update(detections[idet], self.frame_id_)
            activated_starcks.append(unconfirmed[itracked])
            dets_ids[dets_index[idet]] = unconfirmed[itracked].track_id
        for it in u_unconfirmed:
            track = unconfirmed[it]
            track.mark_removed()
            removed_stracks.append(track)

        """ Step 4: Init new stracks"""
        for inew in u_detection:
            track = detections[inew]
            if track.score < self.det_thresh:
                continue
            track.activate_(self.kalman_filter_, self.frame_id_, track_id=self_track_id_ori)
            activated_starcks.append(track)
            dets_ids[dets_index[inew]] = track.track_id
        """ Step 5: Update state"""
        for track in self.lost_stracks_:
            if self.frame_id_ - track.end_frame > self.max_time_lost:
                track.mark_removed()
                removed_stracks.append(track)

        # print('Ramained match {} s'.format(t4-t3))

        self.tracked_stracks_ = [t for t in self.tracked_stracks_ if t.state == TrackState.Tracked]
        self.tracked_stracks_ = joint_stracks(self.tracked_stracks_, activated_starcks)
        self.tracked_stracks_ = joint_stracks(self.tracked_stracks_, refind_stracks)
        self.lost_stracks_ = sub_stracks(self.lost_stracks_, self.tracked_stracks_)
        self.lost_stracks_.extend(lost_stracks)
        self.lost_stracks_ = sub_stracks(self.lost_stracks_, self.removed_stracks_)
        self.removed_stracks_.extend(removed_stracks)
        self.tracked_stracks_, self.lost_stracks_ = remove_duplicate_stracks(self.tracked_stracks_, self.lost_stracks_)
        # get scores of lost tracks
        output_stracks_ori = [track for track in self.tracked_stracks_ if track.is_activated]

        logger.debug('===========Frame {}=========='.format(self.frame_id_))
        logger.debug('Activated: {}'.format([track.track_id for track in activated_starcks]))
        logger.debug('Refind: {}'.format([track.track_id for track in refind_stracks]))
        logger.debug('Lost: {}'.format([track.track_id for track in lost_stracks]))
        logger.debug('Removed: {}'.format([track.track_id for track in removed_stracks]))

        noise = None
        suc = 0
        for attack_ind, track_id in enumerate(dets_ids):
            if track_id == attack_id:
                if self.opt.attack_id > 0:
                    if not hasattr(self, f'frames_{attack_id}'):
                        setattr(self, f'frames_{attack_id}', 0)
                    if getattr(self, f'frames_{attack_id}') < self.FRAME_THR:
                        setattr(self, f'frames_{attack_id}', getattr(self, f'frames_{attack_id}') + 1)
                        break
                fit = self.CheckFit(dets, id_feature, [attack_id], [attack_ind])
                ious = bbox_ious(np.ascontiguousarray(dets[:, :4], dtype=np.float64),
                                 np.ascontiguousarray(dets[:, :4], dtype=np.float64))

                ious[range(len(dets)), range(len(dets))] = 0
                dis = bbox_dis(np.ascontiguousarray(dets[:, :4], dtype=np.float64),
                               np.ascontiguousarray(dets[:, :4], dtype=np.float64))
                dis[range(len(dets)), range(len(dets))] = np.inf
                target_ind = np.argmax(ious[attack_ind])
                if ious[attack_ind][target_ind] >= self.attack_iou_thr:
                    if ious[attack_ind][target_ind] == 0:
                        target_ind = np.argmin(dis[attack_ind])
                    target_id = dets_ids[target_ind]
                    if fit:
                        noise, attack_iter, suc = self.attack_sg_cl(
                            im_blob,
                            img0,
                            id_features,
                            dets,
                            inds,
                            remain_inds,
                            last_info=self.ad_last_info,
                            outputs_ori=output,
                            attack_id=attack_id,
                            attack_ind=attack_ind,
                            target_id=target_id,
                            target_ind=target_ind
                        )
                        self.attack_iou_thr = 0
                        if suc:
                            suc = 1
                            print(
                                f'attack id: {attack_id}\tattack frame {self.frame_id_}: SUCCESS\tl2 distance: {(noise ** 2).sum().sqrt().item()}\titeration: {attack_iter}')
                        else:
                            suc = 2
                            print(
                                f'attack id: {attack_id}\tattack frame {self.frame_id_}: FAIL\tl2 distance: {(noise ** 2).sum().sqrt().item() if noise is not None else None}\titeration: {attack_iter}')
                    else:
                        suc = 3
                    if ious[attack_ind][target_ind] == 0:
                        self.temp_i += 1
                        if self.temp_i >= 10:
                            self.attack_iou_thr = self.ATTACK_IOU_THR
                    else:
                        self.temp_i = 0
                else:
                    self.attack_iou_thr = self.ATTACK_IOU_THR
                    if fit:
                        suc = 2

        if noise is not None:
            l2_dis = (noise ** 2).sum().sqrt().item()
            adImg = torch.clip(im_blob + noise, min=0, max=1)

            noise = self.recoverNoise(noise, img0)
            # adImg = np.clip(img0 + noise, a_min=0, a_max=255)

            # noise = adImg - img0
            noise = (noise - np.min(noise)) / (np.max(noise) - np.min(noise))
            noise = (noise * 255).astype(np.uint8)
        else:
            l2_dis = None
            adImg = im_blob
        output_stracks_att = self.update(adImg, img0, track_id=self_track_id_att)
        adImg = self.recoverNoise(adImg.detach(), img0)
        return output_stracks_ori, output_stracks_att, adImg, noise, l2_dis, suc

    def update_attack_sg_det(self, im_blob, img0, **kwargs):
        self.frame_id_ += 1
        attack_id = kwargs['attack_id']
        self_track_id_ori = kwargs.get('track_id', {}).get('origin', None)
        self_track_id_att = kwargs.get('track_id', {}).get('attack', None)
        activated_starcks = []
        refind_stracks = []
        lost_stracks = []
        removed_stracks = []

        width = img0.shape[1]
        height = img0.shape[0]
        inp_height = im_blob.shape[2]
        inp_width = im_blob.shape[3]
        c = np.array([width / 2., height / 2.], dtype=np.float32)
        s = max(float(inp_width) / float(inp_height) * height, width) * 1.0
        meta = {'c': c, 's': s,
                'out_height': inp_height // self.opt.down_ratio,
                'out_width': inp_width // self.opt.down_ratio}

        ''' Step 1: Network forward, get detections & embeddings'''
        # with torch.no_grad():
        im_blob.requires_grad = True
        self.model.zero_grad()
        output = self.model(im_blob)[-1]
        hm = output['hm'].sigmoid()
        wh = output['wh']
        id_feature = output['id']
        id_feature = F.normalize(id_feature, dim=1)

        reg = output['reg'] if self.opt.reg_offset else None
        dets_raw, inds = mot_decode(hm, wh, reg=reg, cat_spec_wh=self.opt.cat_spec_wh, K=self.opt.K)

        id_features = []
        for i in range(3):
            for j in range(3):
                id_feature_exp = _tranpose_and_gather_feat_expand(id_feature, inds, bias=(i - 1, j - 1)).squeeze(0)
                id_features.append(id_feature_exp)

        id_feature = _tranpose_and_gather_feat_expand(id_feature, inds)

        id_feature = id_feature.squeeze(0)

        dets = self.post_process(dets_raw.clone(), meta)
        dets = self.merge_outputs([dets])[1]

        remain_inds = dets[:, 4] > self.opt.conf_thres
        dets = dets[remain_inds]
        id_feature = id_feature[remain_inds]

        for i in range(len(id_features)):
            id_features[i] = id_features[i][remain_inds]

        id_feature = id_feature.detach().cpu().numpy()

        last_id_features = [None for _ in range(len(dets))]
        last_ad_id_features = [None for _ in range(len(dets))]
        dets_index = [i for i in range(len(dets))]
        dets_ids = [None for _ in range(len(dets))]
        tracks_ad = []

        # import pdb; pdb.set_trace()
        # vis
        '''
        for i in range(0, dets.shape[0]):
            bbox = dets[i][0:4]
            cv2.rectangle(img0, (bbox[0], bbox[1]),
                          (bbox[2], bbox[3]),
                          (0, 255, 0), 2)
        cv2.imshow('dets', img0)
        cv2.waitKey(0)
        id0 = id0-1
        '''

        if len(dets) > 0:
            '''Detections'''
            detections = [STrack(STrack.tlbr_to_tlwh(tlbrs[:4]), tlbrs[4], f, 30) for
                          (tlbrs, f) in zip(dets[:, :5], id_feature)]
        else:
            detections = []

        ''' Add newly detected tracklets to tracked_stracks'''
        unconfirmed = []
        tracked_stracks = []  # type: list[STrack]
        for track in self.tracked_stracks_:
            if not track.is_activated:
                unconfirmed.append(track)
            else:
                tracked_stracks.append(track)

        ''' Step 2: First association, with embedding'''
        strack_pool = joint_stracks(tracked_stracks, self.lost_stracks_)

        STrack.multi_predict(strack_pool)
        dists = matching.embedding_distance(strack_pool, detections)
        # dists = matching.gate_cost_matrix(self.kalman_filter, dists, strack_pool, detections)
        dists = matching.fuse_motion(self.kalman_filter_, dists, strack_pool, detections)
        matches, u_track, u_detection = matching.linear_assignment(dists, thresh=0.7)
        # import pdb; pdb.set_trace()
        for itracked, idet in matches:
            track = strack_pool[itracked]
            det = detections[idet]
            assert last_id_features[dets_index[idet]] is None
            assert last_ad_id_features[dets_index[idet]] is None
            last_id_features[dets_index[idet]] = track.smooth_feat
            last_ad_id_features[dets_index[idet]] = track.smooth_feat_ad
            tracks_ad.append((track, dets_index[idet]))
            if track.state == TrackState.Tracked:
                track.update(detections[idet], self.frame_id_)
                activated_starcks.append(track)
            else:
                track.re_activate_(det, self.frame_id_, new_id=False)
                refind_stracks.append(track)
            dets_ids[dets_index[idet]] = track.track_id

        ''' Step 3: Second association, with IOU'''
        dets_index = [dets_index[i] for i in u_detection]
        detections = [detections[i] for i in u_detection]
        r_tracked_stracks = [strack_pool[i] for i in u_track if strack_pool[i].state == TrackState.Tracked]
        dists = matching.iou_distance(r_tracked_stracks, detections)
        matches, u_track, u_detection = matching.linear_assignment(dists, thresh=0.5)

        for itracked, idet in matches:
            track = r_tracked_stracks[itracked]
            det = detections[idet]
            assert last_id_features[dets_index[idet]] is None
            assert last_ad_id_features[dets_index[idet]] is None
            last_id_features[dets_index[idet]] = track.smooth_feat
            last_ad_id_features[dets_index[idet]] = track.smooth_feat_ad
            tracks_ad.append((track, dets_index[idet]))
            if track.state == TrackState.Tracked:
                track.update(det, self.frame_id_)
                activated_starcks.append(track)
            else:
                track.re_activate_(det, self.frame_id_, new_id=False)
                refind_stracks.append(track)
            dets_ids[dets_index[idet]] = track.track_id

        for it in u_track:
            track = r_tracked_stracks[it]
            if not track.state == TrackState.Lost:
                track.mark_lost()
                lost_stracks.append(track)

        '''Deal with unconfirmed tracks, usually tracks with only one beginning frame'''
        dets_index = [dets_index[i] for i in u_detection]
        detections = [detections[i] for i in u_detection]
        dists = matching.iou_distance(unconfirmed, detections)
        matches, u_unconfirmed, u_detection = matching.linear_assignment(dists, thresh=0.7)
        for itracked, idet in matches:
            assert last_id_features[dets_index[idet]] is None
            assert last_ad_id_features[dets_index[idet]] is None
            last_id_features[dets_index[idet]] = unconfirmed[itracked].smooth_feat
            last_ad_id_features[dets_index[idet]] = unconfirmed[itracked].smooth_feat_ad
            tracks_ad.append((unconfirmed[itracked], dets_index[idet]))
            unconfirmed[itracked].update(detections[idet], self.frame_id_)
            activated_starcks.append(unconfirmed[itracked])
            dets_ids[dets_index[idet]] = unconfirmed[itracked].track_id
        for it in u_unconfirmed:
            track = unconfirmed[it]
            track.mark_removed()
            removed_stracks.append(track)

        """ Step 4: Init new stracks"""
        for inew in u_detection:
            track = detections[inew]
            if track.score < self.det_thresh:
                continue
            track.activate_(self.kalman_filter_, self.frame_id_, track_id=self_track_id_ori)
            activated_starcks.append(track)
            dets_ids[dets_index[inew]] = track.track_id
        """ Step 5: Update state"""
        for track in self.lost_stracks_:
            if self.frame_id_ - track.end_frame > self.max_time_lost:
                track.mark_removed()
                removed_stracks.append(track)

        # print('Ramained match {} s'.format(t4-t3))

        self.tracked_stracks_ = [t for t in self.tracked_stracks_ if t.state == TrackState.Tracked]
        self.tracked_stracks_ = joint_stracks(self.tracked_stracks_, activated_starcks)
        self.tracked_stracks_ = joint_stracks(self.tracked_stracks_, refind_stracks)
        self.lost_stracks_ = sub_stracks(self.lost_stracks_, self.tracked_stracks_)
        self.lost_stracks_.extend(lost_stracks)
        self.lost_stracks_ = sub_stracks(self.lost_stracks_, self.removed_stracks_)
        self.removed_stracks_.extend(removed_stracks)
        self.tracked_stracks_, self.lost_stracks_ = remove_duplicate_stracks(self.tracked_stracks_, self.lost_stracks_)
        # get scores of lost tracks
        output_stracks_ori = [track for track in self.tracked_stracks_ if track.is_activated]

        logger.debug('===========Frame {}=========='.format(self.frame_id_))
        logger.debug('Activated: {}'.format([track.track_id for track in activated_starcks]))
        logger.debug('Refind: {}'.format([track.track_id for track in refind_stracks]))
        logger.debug('Lost: {}'.format([track.track_id for track in lost_stracks]))
        logger.debug('Removed: {}'.format([track.track_id for track in removed_stracks]))

        noise = None
        suc = 0
        for attack_ind, track_id in enumerate(dets_ids):
            if track_id == attack_id:
                if self.opt.attack_id > 0:
                    if not hasattr(self, f'frames_{attack_id}'):
                        setattr(self, f'frames_{attack_id}', 0)
                    if getattr(self, f'frames_{attack_id}') < self.FRAME_THR:
                        setattr(self, f'frames_{attack_id}', getattr(self, f'frames_{attack_id}') + 1)
                        break
                ious = bbox_ious(np.ascontiguousarray(dets[:, :4], dtype=np.float64),
                                 np.ascontiguousarray(dets[:, :4], dtype=np.float64))

                ious = self.processIoUs(ious)
                ious = ious + ious.T
                target_ind = np.argmax(ious[attack_ind])
                if ious[attack_ind][target_ind] >= self.attack_iou_thr:
                    fit = self.CheckFit(dets, id_feature, [attack_id], [attack_ind])
                    if fit:
                        noise, attack_iter, suc = self.attack_sg_det(
                            im_blob,
                            img0,
                            dets,
                            inds,
                            remain_inds,
                            last_info=self.ad_last_info,
                            outputs_ori=output,
                            attack_id=attack_id,
                            attack_ind=attack_ind
                        )
                        self.attack_iou_thr = 0
                        if suc:
                            suc = 1
                            print(
                                f'attack id: {attack_id}\tattack frame {self.frame_id_}: SUCCESS\tl2 distance: {(noise ** 2).sum().sqrt().item()}\titeration: {attack_iter}')
                        else:
                            suc = 2
                            print(
                                f'attack id: {attack_id}\tattack frame {self.frame_id_}: FAIL\tl2 distance: {(noise ** 2).sum().sqrt().item()}\titeration: {attack_iter}')
                    else:
                        suc = 3
                    if ious[attack_ind][target_ind] == 0:
                        self.temp_i += 1
                        if self.temp_i >= 10:
                            self.attack_iou_thr = self.ATTACK_IOU_THR
                    else:
                        self.temp_i = 0
                else:
                    self.attack_iou_thr = self.ATTACK_IOU_THR
                break

        if noise is not None:
            l2_dis = (noise ** 2).sum().sqrt().item()
            adImg = torch.clip(im_blob + noise, min=0, max=1)
            noise = self.recoverNoise(noise, img0)
            noise = (noise - np.min(noise)) / (np.max(noise) - np.min(noise))
            noise = (noise * 255).astype(np.uint8)
        else:
            l2_dis = None
            adImg = im_blob
        output_stracks_att = self.update(adImg, img0, track_id=self_track_id_att)
        adImg = self.recoverNoise(adImg.detach(), img0)

        return output_stracks_ori, output_stracks_att, adImg, noise, l2_dis, suc

    def update_attack_sg_hj(self, im_blob, img0, **kwargs):
        self.frame_id_ += 1
        attack_id = kwargs['attack_id']
        self_track_id_ori = kwargs.get('track_id', {}).get('origin', None)
        self_track_id_att = kwargs.get('track_id', {}).get('attack', None)
        activated_starcks = []
        refind_stracks = []
        lost_stracks = []
        removed_stracks = []

        width = img0.shape[1]
        height = img0.shape[0]
        inp_height = im_blob.shape[2]
        inp_width = im_blob.shape[3]
        c = np.array([width / 2., height / 2.], dtype=np.float32)
        s = max(float(inp_width) / float(inp_height) * height, width) * 1.0
        meta = {'c': c, 's': s,
                'out_height': inp_height // self.opt.down_ratio,
                'out_width': inp_width // self.opt.down_ratio}

        ''' Step 1: Network forward, get detections & embeddings'''
        # with torch.no_grad():
        im_blob.requires_grad = True
        self.model.zero_grad()
        output = self.model(im_blob)[-1]
        hm = output['hm'].sigmoid()
        wh = output['wh']
        id_feature = output['id']
        id_feature = F.normalize(id_feature, dim=1)

        reg = output['reg'] if self.opt.reg_offset else None
        dets_raw, inds = mot_decode(hm, wh, reg=reg, cat_spec_wh=self.opt.cat_spec_wh, K=self.opt.K)

        id_features = []
        for i in range(3):
            for j in range(3):
                id_feature_exp = _tranpose_and_gather_feat_expand(id_feature, inds, bias=(i - 1, j - 1)).squeeze(0)
                id_features.append(id_feature_exp)

        id_feature = _tranpose_and_gather_feat_expand(id_feature, inds)

        id_feature = id_feature.squeeze(0)

        dets = self.post_process(dets_raw.clone(), meta)
        dets = self.merge_outputs([dets])[1]

        remain_inds = dets[:, 4] > self.opt.conf_thres
        dets = dets[remain_inds]
        id_feature = id_feature[remain_inds]

        for i in range(len(id_features)):
            id_features[i] = id_features[i][remain_inds]

        id_feature = id_feature.detach().cpu().numpy()

        last_id_features = [None for _ in range(len(dets))]
        last_ad_id_features = [None for _ in range(len(dets))]
        dets_index = [i for i in range(len(dets))]
        dets_ids = [None for _ in range(len(dets))]
        tracks_ad = []

        # import pdb; pdb.set_trace()
        # vis
        '''
        for i in range(0, dets.shape[0]):
            bbox = dets[i][0:4]
            cv2.rectangle(img0, (bbox[0], bbox[1]),
                          (bbox[2], bbox[3]),
                          (0, 255, 0), 2)
        cv2.imshow('dets', img0)
        cv2.waitKey(0)
        id0 = id0-1
        '''

        if len(dets) > 0:
            '''Detections'''
            detections = [STrack(STrack.tlbr_to_tlwh(tlbrs[:4]), tlbrs[4], f, 30) for
                          (tlbrs, f) in zip(dets[:, :5], id_feature)]
        else:
            detections = []

        ''' Add newly detected tracklets to tracked_stracks'''
        unconfirmed = []
        tracked_stracks = []  # type: list[STrack]
        for track in self.tracked_stracks_:
            if not track.is_activated:
                unconfirmed.append(track)
            else:
                tracked_stracks.append(track)

        ''' Step 2: First association, with embedding'''
        strack_pool = joint_stracks(tracked_stracks, self.lost_stracks_)

        STrack.multi_predict(strack_pool)
        dists = matching.embedding_distance(strack_pool, detections)
        # dists = matching.gate_cost_matrix(self.kalman_filter, dists, strack_pool, detections)
        dists = matching.fuse_motion(self.kalman_filter_, dists, strack_pool, detections)
        matches, u_track, u_detection = matching.linear_assignment(dists, thresh=0.7)
        # import pdb; pdb.set_trace()
        for itracked, idet in matches:
            track = strack_pool[itracked]
            det = detections[idet]
            assert last_id_features[dets_index[idet]] is None
            assert last_ad_id_features[dets_index[idet]] is None
            last_id_features[dets_index[idet]] = track.smooth_feat
            last_ad_id_features[dets_index[idet]] = track.smooth_feat_ad
            tracks_ad.append((track, dets_index[idet]))
            if track.state == TrackState.Tracked:
                track.update(detections[idet], self.frame_id_)
                activated_starcks.append(track)
            else:
                track.re_activate_(det, self.frame_id_, new_id=False)
                refind_stracks.append(track)
            dets_ids[dets_index[idet]] = track.track_id

        ''' Step 3: Second association, with IOU'''
        dets_index = [dets_index[i] for i in u_detection]
        detections = [detections[i] for i in u_detection]
        r_tracked_stracks = [strack_pool[i] for i in u_track if strack_pool[i].state == TrackState.Tracked]
        dists = matching.iou_distance(r_tracked_stracks, detections)
        matches, u_track, u_detection = matching.linear_assignment(dists, thresh=0.5)

        for itracked, idet in matches:
            track = r_tracked_stracks[itracked]
            det = detections[idet]
            assert last_id_features[dets_index[idet]] is None
            assert last_ad_id_features[dets_index[idet]] is None
            last_id_features[dets_index[idet]] = track.smooth_feat
            last_ad_id_features[dets_index[idet]] = track.smooth_feat_ad
            tracks_ad.append((track, dets_index[idet]))
            if track.state == TrackState.Tracked:
                track.update(det, self.frame_id_)
                activated_starcks.append(track)
            else:
                track.re_activate_(det, self.frame_id_, new_id=False)
                refind_stracks.append(track)
            dets_ids[dets_index[idet]] = track.track_id

        for it in u_track:
            track = r_tracked_stracks[it]
            if not track.state == TrackState.Lost:
                track.mark_lost()
                lost_stracks.append(track)

        '''Deal with unconfirmed tracks, usually tracks with only one beginning frame'''
        dets_index = [dets_index[i] for i in u_detection]
        detections = [detections[i] for i in u_detection]
        dists = matching.iou_distance(unconfirmed, detections)
        matches, u_unconfirmed, u_detection = matching.linear_assignment(dists, thresh=0.7)
        for itracked, idet in matches:
            assert last_id_features[dets_index[idet]] is None
            assert last_ad_id_features[dets_index[idet]] is None
            last_id_features[dets_index[idet]] = unconfirmed[itracked].smooth_feat
            last_ad_id_features[dets_index[idet]] = unconfirmed[itracked].smooth_feat_ad
            tracks_ad.append((unconfirmed[itracked], dets_index[idet]))
            unconfirmed[itracked].update(detections[idet], self.frame_id_)
            activated_starcks.append(unconfirmed[itracked])
            dets_ids[dets_index[idet]] = unconfirmed[itracked].track_id
        for it in u_unconfirmed:
            track = unconfirmed[it]
            track.mark_removed()
            removed_stracks.append(track)

        """ Step 4: Init new stracks"""
        for inew in u_detection:
            track = detections[inew]
            if track.score < self.det_thresh:
                continue
            track.activate_(self.kalman_filter_, self.frame_id_, track_id=self_track_id_ori)
            activated_starcks.append(track)
            dets_ids[dets_index[inew]] = track.track_id
        """ Step 5: Update state"""
        for track in self.lost_stracks_:
            if self.frame_id_ - track.end_frame > self.max_time_lost:
                track.mark_removed()
                removed_stracks.append(track)

        # print('Ramained match {} s'.format(t4-t3))

        self.tracked_stracks_ = [t for t in self.tracked_stracks_ if t.state == TrackState.Tracked]
        self.tracked_stracks_ = joint_stracks(self.tracked_stracks_, activated_starcks)
        self.tracked_stracks_ = joint_stracks(self.tracked_stracks_, refind_stracks)
        self.lost_stracks_ = sub_stracks(self.lost_stracks_, self.tracked_stracks_)
        self.lost_stracks_.extend(lost_stracks)
        self.lost_stracks_ = sub_stracks(self.lost_stracks_, self.removed_stracks_)
        self.removed_stracks_.extend(removed_stracks)
        self.tracked_stracks_, self.lost_stracks_ = remove_duplicate_stracks(self.tracked_stracks_, self.lost_stracks_)
        # get scores of lost tracks
        output_stracks_ori = [track for track in self.tracked_stracks_ if track.is_activated]

        logger.debug('===========Frame {}=========='.format(self.frame_id_))
        logger.debug('Activated: {}'.format([track.track_id for track in activated_starcks]))
        logger.debug('Refind: {}'.format([track.track_id for track in refind_stracks]))
        logger.debug('Lost: {}'.format([track.track_id for track in lost_stracks]))
        logger.debug('Removed: {}'.format([track.track_id for track in removed_stracks]))

        noise = None
        suc = 0
        att_tracker = None
        if self.ad_bbox:
            for t in output_stracks_ori:
                if t.track_id == attack_id:
                    att_tracker = t
        for attack_ind, track_id in enumerate(dets_ids):
            if track_id == attack_id:
                if self.opt.attack_id > 0:
                    if not hasattr(self, f'frames_{attack_id}'):
                        setattr(self, f'frames_{attack_id}', 0)
                    if getattr(self, f'frames_{attack_id}') < self.FRAME_THR:
                        setattr(self, f'frames_{attack_id}', getattr(self, f'frames_{attack_id}') + 1)
                        break
                ious = bbox_ious(np.ascontiguousarray(dets[:, :4], dtype=np.float64),
                                 np.ascontiguousarray(dets[:, :4], dtype=np.float64))

                ious = self.processIoUs(ious)
                ious = ious + ious.T
                target_ind = np.argmax(ious[attack_ind])
                if ious[attack_ind][target_ind] >= self.attack_iou_thr:
                    fit = self.CheckFit(dets, id_feature, [attack_id], [attack_ind])
                    if fit:
                        noise, attack_iter, suc = self.attack_sg_hj(
                            im_blob,
                            img0,
                            dets,
                            inds,
                            remain_inds,
                            last_info=self.ad_last_info,
                            outputs_ori=output,
                            attack_id=attack_id,
                            attack_ind=attack_ind,
                            ad_bbox=self.ad_bbox,
                            track_v=att_tracker.get_v() if att_tracker is not None else None
                        )
                        self.attack_iou_thr = 0
                        if suc:
                            suc = 1
                            print(
                                f'attack id: {attack_id}\tattack frame {self.frame_id_}: SUCCESS\tl2 distance: {(noise ** 2).sum().sqrt().item()}\titeration: {attack_iter}')
                        else:
                            suc = 2
                            print(
                                f'attack id: {attack_id}\tattack frame {self.frame_id_}: FAIL\tl2 distance: {(noise ** 2).sum().sqrt().item()}\titeration: {attack_iter}')
                    else:
                        suc = 3
                    if ious[attack_ind][target_ind] == 0:
                        self.temp_i += 1
                        if self.temp_i >= 10:
                            self.attack_iou_thr = self.ATTACK_IOU_THR
                    else:
                        self.temp_i = 0
                else:
                    self.attack_iou_thr = self.ATTACK_IOU_THR
                break

        if noise is not None:
            self.ad_bbox = False
            l2_dis = (noise ** 2).sum().sqrt().item()
            adImg = torch.clip(im_blob + noise, min=0, max=1)
            noise = self.recoverNoise(noise, img0)
            noise = (noise - np.min(noise)) / (np.max(noise) - np.min(noise))
            noise = (noise * 255).astype(np.uint8)
        else:
            l2_dis = None
            adImg = im_blob
        output_stracks_att = self.update(adImg, img0, track_id=self_track_id_att)
        adImg = self.recoverNoise(adImg.detach(), img0)

        return output_stracks_ori, output_stracks_att, adImg, noise, l2_dis, suc

    def update_attack_mt_det(self, im_blob, img0, **kwargs):
        self.frame_id_ += 1
        activated_starcks = []
        refind_stracks = []
        lost_stracks = []
        removed_stracks = []

        width = img0.shape[1]
        height = img0.shape[0]
        inp_height = im_blob.shape[2]
        inp_width = im_blob.shape[3]
        c = np.array([width / 2., height / 2.], dtype=np.float32)
        s = max(float(inp_width) / float(inp_height) * height, width) * 1.0
        meta = {'c': c, 's': s,
                'out_height': inp_height // self.opt.down_ratio,
                'out_width': inp_width // self.opt.down_ratio}

        ''' Step 1: Network forward, get detections & embeddings'''
        # with torch.no_grad():
        im_blob.requires_grad = True
        self.model.zero_grad()
        output = self.model(im_blob)[-1]
        hm = output['hm'].sigmoid()
        wh = output['wh']
        id_feature = output['id']
        id_feature = F.normalize(id_feature, dim=1)

        reg = output['reg'] if self.opt.reg_offset else None
        dets_raw, inds = mot_decode(hm, wh, reg=reg, cat_spec_wh=self.opt.cat_spec_wh, K=self.opt.K)

        id_features = []
        for i in range(3):
            for j in range(3):
                id_feature_exp = _tranpose_and_gather_feat_expand(id_feature, inds, bias=(i - 1, j - 1)).squeeze(0)
                id_features.append(id_feature_exp)

        id_feature = _tranpose_and_gather_feat_expand(id_feature, inds)

        id_feature = id_feature.squeeze(0)

        dets = self.post_process(dets_raw.clone(), meta)
        dets = self.merge_outputs([dets])[1]

        remain_inds = dets[:, 4] > self.opt.conf_thres
        dets = dets[remain_inds]
        id_feature = id_feature[remain_inds]

        for i in range(len(id_features)):
            id_features[i] = id_features[i][remain_inds]

        id_feature = id_feature.detach().cpu().numpy()

        last_id_features = [None for _ in range(len(dets))]
        last_ad_id_features = [None for _ in range(len(dets))]
        dets_index = [i for i in range(len(dets))]
        dets_ids = [None for _ in range(len(dets))]
        tracks_ad = []

        # import pdb; pdb.set_trace()
        # vis
        '''
        for i in range(0, dets.shape[0]):
            bbox = dets[i][0:4]
            cv2.rectangle(img0, (bbox[0], bbox[1]),
                          (bbox[2], bbox[3]),
                          (0, 255, 0), 2)
        cv2.imshow('dets', img0)
        cv2.waitKey(0)
        id0 = id0-1
        '''

        if len(dets) > 0:
            '''Detections'''
            detections = [STrack(STrack.tlbr_to_tlwh(tlbrs[:4]), tlbrs[4], f, 30) for
                          (tlbrs, f) in zip(dets[:, :5], id_feature)]
        else:
            detections = []

        ''' Add newly detected tracklets to tracked_stracks'''
        unconfirmed = []
        tracked_stracks = []  # type: list[STrack]
        for track in self.tracked_stracks_:
            if not track.is_activated:
                unconfirmed.append(track)
            else:
                tracked_stracks.append(track)

        ''' Step 2: First association, with embedding'''
        strack_pool = joint_stracks(tracked_stracks, self.lost_stracks_)

        STrack.multi_predict(strack_pool)
        dists = matching.embedding_distance(strack_pool, detections)
        dists = matching.fuse_motion(self.kalman_filter_, dists, strack_pool, detections)
        matches, u_track, u_detection = matching.linear_assignment(dists, thresh=0.7)
        # import pdb; pdb.set_trace()
        for itracked, idet in matches:
            track = strack_pool[itracked]
            det = detections[idet]
            assert last_id_features[dets_index[idet]] is None
            assert last_ad_id_features[dets_index[idet]] is None
            last_id_features[dets_index[idet]] = track.smooth_feat
            last_ad_id_features[dets_index[idet]] = track.smooth_feat_ad
            tracks_ad.append((track, dets_index[idet]))
            if track.state == TrackState.Tracked:
                track.update(detections[idet], self.frame_id_)
                activated_starcks.append(track)
            else:
                track.re_activate_(det, self.frame_id_, new_id=False)
                refind_stracks.append(track)
            dets_ids[dets_index[idet]] = track.track_id

        ''' Step 3: Second association, with IOU'''
        dets_index = [dets_index[i] for i in u_detection]
        detections = [detections[i] for i in u_detection]
        r_tracked_stracks = [strack_pool[i] for i in u_track if strack_pool[i].state == TrackState.Tracked]
        dists = matching.iou_distance(r_tracked_stracks, detections)
        matches, u_track, u_detection = matching.linear_assignment(dists, thresh=0.5)

        for itracked, idet in matches:
            track = r_tracked_stracks[itracked]
            det = detections[idet]
            assert last_id_features[dets_index[idet]] is None
            assert last_ad_id_features[dets_index[idet]] is None
            last_id_features[dets_index[idet]] = track.smooth_feat
            last_ad_id_features[dets_index[idet]] = track.smooth_feat_ad
            tracks_ad.append((track, dets_index[idet]))
            if track.state == TrackState.Tracked:
                track.update(det, self.frame_id_)
                activated_starcks.append(track)
            else:
                track.re_activate_(det, self.frame_id_, new_id=False)
                refind_stracks.append(track)
            dets_ids[dets_index[idet]] = track.track_id

        for it in u_track:
            track = r_tracked_stracks[it]
            if not track.state == TrackState.Lost:
                track.mark_lost()
                lost_stracks.append(track)

        '''Deal with unconfirmed tracks, usually tracks with only one beginning frame'''
        dets_index = [dets_index[i] for i in u_detection]
        detections = [detections[i] for i in u_detection]
        dists = matching.iou_distance(unconfirmed, detections)
        matches, u_unconfirmed, u_detection = matching.linear_assignment(dists, thresh=0.7)
        for itracked, idet in matches:
            assert last_id_features[dets_index[idet]] is None
            assert last_ad_id_features[dets_index[idet]] is None
            last_id_features[dets_index[idet]] = unconfirmed[itracked].smooth_feat
            last_ad_id_features[dets_index[idet]] = unconfirmed[itracked].smooth_feat_ad
            tracks_ad.append((unconfirmed[itracked], dets_index[idet]))
            unconfirmed[itracked].update(detections[idet], self.frame_id_)
            activated_starcks.append(unconfirmed[itracked])
            dets_ids[dets_index[idet]] = unconfirmed[itracked].track_id

        for it in u_unconfirmed:
            track = unconfirmed[it]
            track.mark_removed()
            removed_stracks.append(track)

        """ Step 4: Init new stracks"""
        for inew in u_detection:
            track = detections[inew]
            if track.score < self.det_thresh:
                continue
            track.activate_(self.kalman_filter_, self.frame_id_)
            activated_starcks.append(track)
            dets_ids[dets_index[inew]] = track.track_id

        """ Step 5: Update state"""
        for track in self.lost_stracks_:
            if self.frame_id_ - track.end_frame > self.max_time_lost:
                track.mark_removed()
                removed_stracks.append(track)

        # print('Ramained match {} s'.format(t4-t3))

        self.tracked_stracks_ = [t for t in self.tracked_stracks_ if t.state == TrackState.Tracked]
        self.tracked_stracks_ = joint_stracks(self.tracked_stracks_, activated_starcks)
        self.tracked_stracks_ = joint_stracks(self.tracked_stracks_, refind_stracks)
        self.lost_stracks_ = sub_stracks(self.lost_stracks_, self.tracked_stracks_)
        self.lost_stracks_.extend(lost_stracks)
        self.lost_stracks_ = sub_stracks(self.lost_stracks_, self.removed_stracks_)
        self.removed_stracks_.extend(removed_stracks)
        self.tracked_stracks_, self.lost_stracks_ = remove_duplicate_stracks(self.tracked_stracks_, self.lost_stracks_)
        # get scores of lost tracks
        output_stracks_ori = [track for track in self.tracked_stracks_ if track.is_activated]
        id_set = set([track.track_id for track in output_stracks_ori])
        for i in range(len(dets_ids)):
            if dets_ids[i] is not None and dets_ids[i] not in id_set:
                dets_ids[i] = None

        output_stracks_ori_ind = []
        for ind, track in enumerate(output_stracks_ori):
            if track.track_id not in self.multiple_ori_ids:
                self.multiple_ori_ids[track.track_id] = 0
            self.multiple_ori_ids[track.track_id] += 1
            if self.multiple_ori_ids[track.track_id] <= self.FRAME_THR:
                output_stracks_ori_ind.append(ind)

        logger.debug('===========Frame {}=========='.format(self.frame_id_))
        logger.debug('Activated: {}'.format([track.track_id for track in activated_starcks]))
        logger.debug('Refind: {}'.format([track.track_id for track in refind_stracks]))
        logger.debug('Lost: {}'.format([track.track_id for track in lost_stracks]))
        logger.debug('Removed: {}'.format([track.track_id for track in removed_stracks]))

        attack_ids = []
        target_ids = []
        attack_inds = []
        target_inds = []

        noise = None
        if len(dets) > 0:
            ious = bbox_ious(np.ascontiguousarray(dets[:, :4], dtype=np.float64),
                             np.ascontiguousarray(dets[:, :4], dtype=np.float64))
            ious[range(len(dets)), range(len(dets))] = 0
            ious_inds = np.argmax(ious, axis=1)
            dis = bbox_dis(np.ascontiguousarray(dets[:, :4], dtype=np.float64),
                           np.ascontiguousarray(dets[:, :4], dtype=np.float64))
            dis[range(len(dets)), range(len(dets))] = np.inf
            dis_inds = np.argmin(dis, axis=1)
            for attack_ind, track_id in enumerate(dets_ids):
                if track_id is None or self.multiple_ori_ids[track_id] <= self.FRAME_THR \
                        or dets_ids[ious_inds[attack_ind]] not in self.multiple_ori2att \
                        or track_id not in self.multiple_ori2att:
                    continue
                if ious[attack_ind, ious_inds[attack_ind]] > self.ATTACK_IOU_THR or (
                        track_id in self.low_iou_ids and ious[attack_ind, ious_inds[attack_ind]] > 0
                ):
                    attack_ids.append(track_id)
                    target_ids.append(dets_ids[ious_inds[attack_ind]])
                    attack_inds.append(attack_ind)
                    target_inds.append(ious_inds[attack_ind])
                    if hasattr(self, f'temp_i_{track_id}'):
                        self.__setattr__(f'temp_i_{track_id}', 0)
                elif ious[attack_ind, ious_inds[attack_ind]] == 0 and track_id in self.low_iou_ids:
                    if hasattr(self, f'temp_i_{track_id}'):
                        self.__setattr__(f'temp_i_{track_id}', self.__getattribute__(f'temp_i_{track_id}') + 1)
                    else:
                        self.__setattr__(f'temp_i_{track_id}', 1)
                    if self.__getattribute__(f'temp_i_{track_id}') > 10:
                        self.low_iou_ids.remove(track_id)
                    elif dets_ids[dis_inds[attack_ind]] in self.multiple_ori2att:
                        attack_ids.append(track_id)
                        target_ids.append(dets_ids[dis_inds[attack_ind]])
                        attack_inds.append(attack_ind)
                        target_inds.append(dis_inds[attack_ind])
            fit_index = self.CheckFit(dets, id_feature, attack_ids, attack_inds) if len(attack_ids) else []
            if fit_index:
                attack_ids = np.array(attack_ids)[fit_index]
                target_ids = np.array(target_ids)[fit_index]
                attack_inds = np.array(attack_inds)[fit_index]
                target_inds = np.array(target_inds)[fit_index]

                noise, attack_iter, suc = self.attack_mt_det(
                    im_blob,
                    img0,
                    dets,
                    inds,
                    remain_inds,
                    last_info=self.ad_last_info,
                    outputs_ori=output,
                    attack_ids=attack_ids,
                    attack_inds=attack_inds
                )
                self.low_iou_ids.update(set(attack_ids))
                if suc:
                    self.attacked_ids.update(set(attack_ids))
                    print(
                        f'attack ids: {attack_ids}\tattack frame {self.frame_id_}: SUCCESS\tl2 distance: {(noise ** 2).sum().sqrt().item()}\titeration: {attack_iter}')
                else:
                    print(f'attack ids: {attack_ids}\tattack frame {self.frame_id_}: FAIL\tl2 distance: {(noise ** 2).sum().sqrt().item() if noise is not None else None}\titeration: {attack_iter}')

        if noise is not None:
            l2_dis = (noise ** 2).sum().sqrt().item()
            adImg = torch.clip(im_blob + noise, min=0, max=1)

            noise = self.recoverNoise(noise, img0)
            noise = (noise - np.min(noise)) / (np.max(noise) - np.min(noise))
            noise = (noise * 255).astype(np.uint8)
        else:
            l2_dis = None
            adImg = im_blob

        output_stracks_att = self.update(adImg, img0)
        adImg = self.recoverNoise(adImg.detach(), img0)

        output_stracks_att_ind = []
        for ind, track in enumerate(output_stracks_att):
            if track.track_id not in self.multiple_att_ids:
                self.multiple_att_ids[track.track_id] = 0
            self.multiple_att_ids[track.track_id] += 1
            if self.multiple_att_ids[track.track_id] <= self.FRAME_THR:
                output_stracks_att_ind.append(ind)
        if len(output_stracks_ori_ind) and len(output_stracks_att_ind):
            ori_dets = [track.curr_tlbr for i, track in enumerate(output_stracks_ori) if i in output_stracks_ori_ind]
            att_dets = [track.curr_tlbr for i, track in enumerate(output_stracks_att) if i in output_stracks_att_ind]
            ori_dets = np.stack(ori_dets).astype(np.float64)
            att_dets = np.stack(att_dets).astype(np.float64)
            ious = bbox_ious(ori_dets, att_dets)
            row_ind, col_ind = linear_sum_assignment(-ious)
            for i in range(len(row_ind)):
                if ious[row_ind[i], col_ind[i]] > 0.9:
                    ori_id = output_stracks_ori[output_stracks_ori_ind[row_ind[i]]].track_id
                    att_id = output_stracks_att[output_stracks_att_ind[col_ind[i]]].track_id
                    self.multiple_ori2att[ori_id] = att_id
        return output_stracks_ori, output_stracks_att, adImg, noise, l2_dis

    def update_attack_mt_hj(self, im_blob, img0, **kwargs):
        self.frame_id_ += 1
        activated_starcks = []
        refind_stracks = []
        lost_stracks = []
        removed_stracks = []

        width = img0.shape[1]
        height = img0.shape[0]
        inp_height = im_blob.shape[2]
        inp_width = im_blob.shape[3]
        c = np.array([width / 2., height / 2.], dtype=np.float32)
        s = max(float(inp_width) / float(inp_height) * height, width) * 1.0
        meta = {'c': c, 's': s,
                'out_height': inp_height // self.opt.down_ratio,
                'out_width': inp_width // self.opt.down_ratio}

        ''' Step 1: Network forward, get detections & embeddings'''
        # with torch.no_grad():
        im_blob.requires_grad = True
        self.model.zero_grad()
        output = self.model(im_blob)[-1]
        hm = output['hm'].sigmoid()
        wh = output['wh']
        id_feature = output['id']
        id_feature = F.normalize(id_feature, dim=1)

        reg = output['reg'] if self.opt.reg_offset else None
        dets_raw, inds = mot_decode(hm, wh, reg=reg, cat_spec_wh=self.opt.cat_spec_wh, K=self.opt.K)

        id_features = []
        for i in range(3):
            for j in range(3):
                id_feature_exp = _tranpose_and_gather_feat_expand(id_feature, inds, bias=(i - 1, j - 1)).squeeze(0)
                id_features.append(id_feature_exp)

        id_feature = _tranpose_and_gather_feat_expand(id_feature, inds)

        id_feature = id_feature.squeeze(0)

        dets = self.post_process(dets_raw.clone(), meta)
        dets = self.merge_outputs([dets])[1]

        remain_inds = dets[:, 4] > self.opt.conf_thres
        dets = dets[remain_inds]
        id_feature = id_feature[remain_inds]

        for i in range(len(id_features)):
            id_features[i] = id_features[i][remain_inds]

        id_feature = id_feature.detach().cpu().numpy()

        last_id_features = [None for _ in range(len(dets))]
        last_ad_id_features = [None for _ in range(len(dets))]
        dets_index = [i for i in range(len(dets))]
        dets_ids = [None for _ in range(len(dets))]
        tracks_ad = []

        # import pdb; pdb.set_trace()
        # vis
        '''
        for i in range(0, dets.shape[0]):
            bbox = dets[i][0:4]
            cv2.rectangle(img0, (bbox[0], bbox[1]),
                          (bbox[2], bbox[3]),
                          (0, 255, 0), 2)
        cv2.imshow('dets', img0)
        cv2.waitKey(0)
        id0 = id0-1
        '''

        if len(dets) > 0:
            '''Detections'''
            detections = [STrack(STrack.tlbr_to_tlwh(tlbrs[:4]), tlbrs[4], f, 30) for
                          (tlbrs, f) in zip(dets[:, :5], id_feature)]
        else:
            detections = []

        ''' Add newly detected tracklets to tracked_stracks'''
        unconfirmed = []
        tracked_stracks = []  # type: list[STrack]
        for track in self.tracked_stracks_:
            if not track.is_activated:
                unconfirmed.append(track)
            else:
                tracked_stracks.append(track)

        ''' Step 2: First association, with embedding'''
        strack_pool = joint_stracks(tracked_stracks, self.lost_stracks_)

        STrack.multi_predict(strack_pool)
        dists = matching.embedding_distance(strack_pool, detections)
        dists = matching.fuse_motion(self.kalman_filter_, dists, strack_pool, detections)
        matches, u_track, u_detection = matching.linear_assignment(dists, thresh=0.7)
        # import pdb; pdb.set_trace()
        for itracked, idet in matches:
            track = strack_pool[itracked]
            det = detections[idet]
            assert last_id_features[dets_index[idet]] is None
            assert last_ad_id_features[dets_index[idet]] is None
            last_id_features[dets_index[idet]] = track.smooth_feat
            last_ad_id_features[dets_index[idet]] = track.smooth_feat_ad
            tracks_ad.append((track, dets_index[idet]))
            if track.state == TrackState.Tracked:
                track.update(detections[idet], self.frame_id_)
                activated_starcks.append(track)
            else:
                track.re_activate_(det, self.frame_id_, new_id=False)
                refind_stracks.append(track)
            dets_ids[dets_index[idet]] = track.track_id

        ''' Step 3: Second association, with IOU'''
        dets_index = [dets_index[i] for i in u_detection]
        detections = [detections[i] for i in u_detection]
        r_tracked_stracks = [strack_pool[i] for i in u_track if strack_pool[i].state == TrackState.Tracked]
        dists = matching.iou_distance(r_tracked_stracks, detections)
        matches, u_track, u_detection = matching.linear_assignment(dists, thresh=0.5)

        for itracked, idet in matches:
            track = r_tracked_stracks[itracked]
            det = detections[idet]
            assert last_id_features[dets_index[idet]] is None
            assert last_ad_id_features[dets_index[idet]] is None
            last_id_features[dets_index[idet]] = track.smooth_feat
            last_ad_id_features[dets_index[idet]] = track.smooth_feat_ad
            tracks_ad.append((track, dets_index[idet]))
            if track.state == TrackState.Tracked:
                track.update(det, self.frame_id_)
                activated_starcks.append(track)
            else:
                track.re_activate_(det, self.frame_id_, new_id=False)
                refind_stracks.append(track)
            dets_ids[dets_index[idet]] = track.track_id

        for it in u_track:
            track = r_tracked_stracks[it]
            if not track.state == TrackState.Lost:
                track.mark_lost()
                lost_stracks.append(track)

        '''Deal with unconfirmed tracks, usually tracks with only one beginning frame'''
        dets_index = [dets_index[i] for i in u_detection]
        detections = [detections[i] for i in u_detection]
        dists = matching.iou_distance(unconfirmed, detections)
        matches, u_unconfirmed, u_detection = matching.linear_assignment(dists, thresh=0.7)
        for itracked, idet in matches:
            assert last_id_features[dets_index[idet]] is None
            assert last_ad_id_features[dets_index[idet]] is None
            last_id_features[dets_index[idet]] = unconfirmed[itracked].smooth_feat
            last_ad_id_features[dets_index[idet]] = unconfirmed[itracked].smooth_feat_ad
            tracks_ad.append((unconfirmed[itracked], dets_index[idet]))
            unconfirmed[itracked].update(detections[idet], self.frame_id_)
            activated_starcks.append(unconfirmed[itracked])
            dets_ids[dets_index[idet]] = unconfirmed[itracked].track_id

        for it in u_unconfirmed:
            track = unconfirmed[it]
            track.mark_removed()
            removed_stracks.append(track)

        """ Step 4: Init new stracks"""
        for inew in u_detection:
            track = detections[inew]
            if track.score < self.det_thresh:
                continue
            track.activate_(self.kalman_filter_, self.frame_id_)
            activated_starcks.append(track)
            dets_ids[dets_index[inew]] = track.track_id

        """ Step 5: Update state"""
        for track in self.lost_stracks_:
            if self.frame_id_ - track.end_frame > self.max_time_lost:
                track.mark_removed()
                removed_stracks.append(track)

        # print('Ramained match {} s'.format(t4-t3))

        self.tracked_stracks_ = [t for t in self.tracked_stracks_ if t.state == TrackState.Tracked]
        self.tracked_stracks_ = joint_stracks(self.tracked_stracks_, activated_starcks)
        self.tracked_stracks_ = joint_stracks(self.tracked_stracks_, refind_stracks)
        self.lost_stracks_ = sub_stracks(self.lost_stracks_, self.tracked_stracks_)
        self.lost_stracks_.extend(lost_stracks)
        self.lost_stracks_ = sub_stracks(self.lost_stracks_, self.removed_stracks_)
        self.removed_stracks_.extend(removed_stracks)
        self.tracked_stracks_, self.lost_stracks_ = remove_duplicate_stracks(self.tracked_stracks_, self.lost_stracks_)
        # get scores of lost tracks
        output_stracks_ori = [track for track in self.tracked_stracks_ if track.is_activated]
        id_set = set([track.track_id for track in output_stracks_ori])
        for i in range(len(dets_ids)):
            if dets_ids[i] is not None and dets_ids[i] not in id_set:
                dets_ids[i] = None

        output_stracks_ori_ind = []
        for ind, track in enumerate(output_stracks_ori):
            if track.track_id not in self.multiple_ori_ids:
                self.multiple_ori_ids[track.track_id] = 0
            self.multiple_ori_ids[track.track_id] += 1
            if self.multiple_ori_ids[track.track_id] <= self.FRAME_THR:
                output_stracks_ori_ind.append(ind)

        logger.debug('===========Frame {}=========='.format(self.frame_id_))
        logger.debug('Activated: {}'.format([track.track_id for track in activated_starcks]))
        logger.debug('Refind: {}'.format([track.track_id for track in refind_stracks]))
        logger.debug('Lost: {}'.format([track.track_id for track in lost_stracks]))
        logger.debug('Removed: {}'.format([track.track_id for track in removed_stracks]))

        attack_ids = []
        target_ids = []
        attack_inds = []
        target_inds = []

        noise = None
        if len(dets) > 0:
            ious = bbox_ious(np.ascontiguousarray(dets[:, :4], dtype=np.float64),
                             np.ascontiguousarray(dets[:, :4], dtype=np.float64))
            ious[range(len(dets)), range(len(dets))] = 0
            ious_inds = np.argmax(ious, axis=1)
            dis = bbox_dis(np.ascontiguousarray(dets[:, :4], dtype=np.float64),
                           np.ascontiguousarray(dets[:, :4], dtype=np.float64))
            dis[range(len(dets)), range(len(dets))] = np.inf
            dis_inds = np.argmin(dis, axis=1)
            for attack_ind, track_id in enumerate(dets_ids):
                if track_id is None or self.multiple_ori_ids[track_id] <= self.FRAME_THR \
                        or dets_ids[ious_inds[attack_ind]] not in self.multiple_ori2att \
                        or track_id not in self.multiple_ori2att:
                    continue
                if ious[attack_ind, ious_inds[attack_ind]] > self.ATTACK_IOU_THR or (
                        track_id in self.low_iou_ids and ious[attack_ind, ious_inds[attack_ind]] > 0
                ):
                    attack_ids.append(track_id)
                    target_ids.append(dets_ids[ious_inds[attack_ind]])
                    attack_inds.append(attack_ind)
                    target_inds.append(ious_inds[attack_ind])
                    if hasattr(self, f'temp_i_{track_id}'):
                        self.__setattr__(f'temp_i_{track_id}', 0)
                elif ious[attack_ind, ious_inds[attack_ind]] == 0 and track_id in self.low_iou_ids:
                    if hasattr(self, f'temp_i_{track_id}'):
                        self.__setattr__(f'temp_i_{track_id}', self.__getattribute__(f'temp_i_{track_id}') + 1)
                    else:
                        self.__setattr__(f'temp_i_{track_id}', 1)
                    if self.__getattribute__(f'temp_i_{track_id}') > 10:
                        self.low_iou_ids.remove(track_id)
                    elif dets_ids[dis_inds[attack_ind]] in self.multiple_ori2att:
                        attack_ids.append(track_id)
                        target_ids.append(dets_ids[dis_inds[attack_ind]])
                        attack_inds.append(attack_ind)
                        target_inds.append(dis_inds[attack_ind])
            fit_index = self.CheckFit(dets, id_feature, attack_ids, attack_inds) if len(attack_ids) else []
            if fit_index:
                attack_ids = np.array(attack_ids)[fit_index]
                target_ids = np.array(target_ids)[fit_index]
                attack_inds = np.array(attack_inds)[fit_index]
                target_inds = np.array(target_inds)[fit_index]

                att_trackers = []
                for attack_id in attack_ids:
                    if attack_id not in self.ad_ids:
                        for t in output_stracks_ori:
                            if t.track_id == attack_id:
                                att_trackers.append(t)

                noise, attack_iter, suc = self.attack_mt_hj(
                    im_blob,
                    img0,
                    dets,
                    inds,
                    remain_inds,
                    last_info=self.ad_last_info,
                    outputs_ori=output,
                    attack_ids=attack_ids,
                    attack_inds=attack_inds,
                    ad_ids=self.ad_ids,
                    track_vs=[t.get_v() for t in att_trackers]
                )
                self.ad_ids.update(attack_ids)
                self.low_iou_ids.update(set(attack_ids))
                if suc:
                    self.attacked_ids.update(set(attack_ids))
                    print(
                        f'attack ids: {attack_ids}\tattack frame {self.frame_id_}: SUCCESS\tl2 distance: {(noise ** 2).sum().sqrt().item()}\titeration: {attack_iter}')
                else:
                    print(f'attack ids: {attack_ids}\tattack frame {self.frame_id_}: FAIL\tl2 distance: {(noise ** 2).sum().sqrt().item() if noise is not None else None}\titeration: {attack_iter}')

        if noise is not None:
            l2_dis = (noise ** 2).sum().sqrt().item()
            adImg = torch.clip(im_blob + noise, min=0, max=1)

            noise = self.recoverNoise(noise, img0)
            noise = (noise - np.min(noise)) / (np.max(noise) - np.min(noise))
            noise = (noise * 255).astype(np.uint8)
        else:
            l2_dis = None
            adImg = im_blob

        output_stracks_att = self.update(adImg, img0)
        adImg = self.recoverNoise(adImg.detach(), img0)

        output_stracks_att_ind = []
        for ind, track in enumerate(output_stracks_att):
            if track.track_id not in self.multiple_att_ids:
                self.multiple_att_ids[track.track_id] = 0
            self.multiple_att_ids[track.track_id] += 1
            if self.multiple_att_ids[track.track_id] <= self.FRAME_THR:
                output_stracks_att_ind.append(ind)
        if len(output_stracks_ori_ind) and len(output_stracks_att_ind):
            ori_dets = [track.curr_tlbr for i, track in enumerate(output_stracks_ori) if i in output_stracks_ori_ind]
            att_dets = [track.curr_tlbr for i, track in enumerate(output_stracks_att) if i in output_stracks_att_ind]
            ori_dets = np.stack(ori_dets).astype(np.float64)
            att_dets = np.stack(att_dets).astype(np.float64)
            ious = bbox_ious(ori_dets, att_dets)
            row_ind, col_ind = linear_sum_assignment(-ious)
            for i in range(len(row_ind)):
                if ious[row_ind[i], col_ind[i]] > 0.9:
                    ori_id = output_stracks_ori[output_stracks_ori_ind[row_ind[i]]].track_id
                    att_id = output_stracks_att[output_stracks_att_ind[col_ind[i]]].track_id
                    self.multiple_ori2att[ori_id] = att_id
        return output_stracks_ori, output_stracks_att, adImg, noise, l2_dis

    def update(self, im_blob, img0, **kwargs):
        self.frame_id += 1
        self_track_id = kwargs.get('track_id', None)
        activated_starcks = []
        refind_stracks = []
        lost_stracks = []
        removed_stracks = []

        width = img0.shape[1]
        height = img0.shape[0]
        inp_height = im_blob.shape[2]
        inp_width = im_blob.shape[3]
        c = np.array([width / 2., height / 2.], dtype=np.float32)
        s = max(float(inp_width) / float(inp_height) * height, width) * 1.0
        meta = {'c': c, 's': s,
                'out_height': inp_height // self.opt.down_ratio,
                'out_width': inp_width // self.opt.down_ratio}

        ''' Step 1: Network forward, get detections & embeddings'''
        with torch.no_grad():
            output = self.model(im_blob)[-1]
            hm = output['hm'].sigmoid_()
            wh = output['wh']
            id_feature = output['id']
            id_feature = F.normalize(id_feature, dim=1)

            reg = output['reg'] if self.opt.reg_offset else None
            dets, inds = mot_decode(hm, wh, reg=reg, cat_spec_wh=self.opt.cat_spec_wh, K=self.opt.K)
            id_feature_ = id_feature.permute(0, 2, 3, 1).view(-1, 512)
            id_feature = _tranpose_and_gather_feat(id_feature, inds)
            id_feature = id_feature.squeeze(0)
            id_feature = id_feature.detach().cpu().numpy()

        dets = self.post_process(dets, meta)
        dets = self.merge_outputs([dets])[1]

        remain_inds = dets[:, 4] > self.opt.conf_thres
        dets = dets[remain_inds]
        id_feature = id_feature[remain_inds]
        # import pdb; pdb.set_trace()
        dets_index = inds[0][remain_inds].tolist()

        # vis
        '''
        for i in range(0, dets.shape[0]):
            bbox = dets[i][0:4]
            cv2.rectangle(img0, (bbox[0], bbox[1]),
                          (bbox[2], bbox[3]),
                          (0, 255, 0), 2)
        cv2.imshow('dets', img0)
        cv2.waitKey(0)
        id0 = id0-1
        '''

        if len(dets) > 0:
            '''Detections'''
            detections = [STrack(STrack.tlbr_to_tlwh(tlbrs[:4]), tlbrs[4], f, 30) for
                          (tlbrs, f) in zip(dets[:, :5], id_feature)]
        else:
            detections = []

        ''' Add newly detected tracklets to tracked_stracks'''
        unconfirmed = []
        tracked_stracks = []  # type: list[STrack]
        for track in self.tracked_stracks:
            if not track.is_activated:
                unconfirmed.append(track)
            else:
                tracked_stracks.append(track)

        ''' Step 2: First association, with embedding'''
        strack_pool = joint_stracks(tracked_stracks, self.lost_stracks)

        # Predict the current location with KF
        # for strack in strack_pool:
        # strack.predict()
        STrack.multi_predict(strack_pool)
        dists = matching.embedding_distance(strack_pool, detections)
        dists = matching.fuse_motion(self.kalman_filter, dists, strack_pool, detections)
        matches, u_track, u_detection = matching.linear_assignment(dists, thresh=0.7)

        for itracked, idet in matches:
            track = strack_pool[itracked]
            det = detections[idet]
            if track.state == TrackState.Tracked:
                track.update(detections[idet], self.frame_id)
                activated_starcks.append(track)
            else:
                track.re_activate(det, self.frame_id, new_id=False)
                refind_stracks.append(track)

        ''' Step 3: Second association, with IOU'''
        dets_index = [dets_index[i] for i in u_detection]
        detections = [detections[i] for i in u_detection]
        r_tracked_stracks = [strack_pool[i] for i in u_track if strack_pool[i].state == TrackState.Tracked]
        dists = matching.iou_distance(r_tracked_stracks, detections)
        matches, u_track, u_detection = matching.linear_assignment(dists, thresh=0.5)

        for itracked, idet in matches:
            track = r_tracked_stracks[itracked]
            det = detections[idet]
            if track.state == TrackState.Tracked:
                track.update(det, self.frame_id)
                activated_starcks.append(track)
            else:
                track.re_activate(det, self.frame_id, new_id=False)
                refind_stracks.append(track)

        for it in u_track:
            track = r_tracked_stracks[it]
            if not track.state == TrackState.Lost:
                track.mark_lost()
                lost_stracks.append(track)

        '''Deal with unconfirmed tracks, usually tracks with only one beginning frame'''
        dets_index = [dets_index[i] for i in u_detection]
        detections = [detections[i] for i in u_detection]
        dists = matching.iou_distance(unconfirmed, detections)
        matches, u_unconfirmed, u_detection = matching.linear_assignment(dists, thresh=0.7)
        for itracked, idet in matches:
            unconfirmed[itracked].update(detections[idet], self.frame_id)
            activated_starcks.append(unconfirmed[itracked])
        for it in u_unconfirmed:
            track = unconfirmed[it]
            track.mark_removed()
            removed_stracks.append(track)

        """ Step 4: Init new stracks"""
        for inew in u_detection:
            track = detections[inew]
            if track.score < self.det_thresh:
                continue
            track.activate(self.kalman_filter, self.frame_id, track_id=self_track_id)
            activated_starcks.append(track)
        """ Step 5: Update state"""
        for track in self.lost_stracks:
            if self.frame_id - track.end_frame > self.max_time_lost:
                track.mark_removed()
                removed_stracks.append(track)

        # print('Ramained match {} s'.format(t4-t3))

        self.tracked_stracks = [t for t in self.tracked_stracks if t.state == TrackState.Tracked]
        self.tracked_stracks = joint_stracks(self.tracked_stracks, activated_starcks)
        self.tracked_stracks = joint_stracks(self.tracked_stracks, refind_stracks)
        self.lost_stracks = sub_stracks(self.lost_stracks, self.tracked_stracks)
        self.lost_stracks.extend(lost_stracks)
        self.lost_stracks = sub_stracks(self.lost_stracks, self.removed_stracks)
        self.removed_stracks.extend(removed_stracks)
        self.tracked_stracks, self.lost_stracks = remove_duplicate_stracks(self.tracked_stracks, self.lost_stracks)
        # get scores of lost tracks
        output_stracks = [track for track in self.tracked_stracks if track.is_activated]

        logger.debug('===========Frame {}=========='.format(self.frame_id))
        logger.debug('Activated: {}'.format([track.track_id for track in activated_starcks]))
        logger.debug('Refind: {}'.format([track.track_id for track in refind_stracks]))
        logger.debug('Lost: {}'.format([track.track_id for track in lost_stracks]))
        logger.debug('Removed: {}'.format([track.track_id for track in removed_stracks]))

        unconfirmed = []
        tracked_stracks = []  # type: list[STrack]
        for track in self.tracked_stracks:
            if not track.is_activated:
                unconfirmed.append(track)
            else:
                tracked_stracks.append(track)

        ''' Step 2: First association, with embedding'''
        strack_pool = joint_stracks(tracked_stracks, self.lost_stracks)

        self.ad_last_info = {
            'last_strack_pool': copy.deepcopy(strack_pool),
            'last_unconfirmed': copy.deepcopy(unconfirmed),
            'kalman_filter': copy.deepcopy(self.kalman_filter_)
        }

        return output_stracks

    def _nms(self, heat, kernel=3):

        pad = (kernel - 1) // 2
        hmax = nn.functional.max_pool2d(
            heat, (kernel, kernel), stride=1, padding=pad)
        keep = (hmax == heat).float
        return keep

    def computer_targets(self, boxes, gt_box):
        an_ws = boxes[:, 2]
        an_hs = boxes[:, 3]
        ctr_x = boxes[:, 0]
        ctr_y = boxes[:, 1]

        gt_ws = gt_box[:, 2]
        gt_hs = gt_box[:, 3]
        gt_ctr_x = gt_box[:, 0]
        gt_ctr_y = gt_box[:, 1]

        targets_dx = (gt_ctr_x - ctr_x) / an_ws
        targets_dy = (gt_ctr_y - ctr_y) / an_hs
        targets_dw = np.log(gt_ws / an_ws)
        targets_dh = np.log(gt_hs / an_hs)

        targets = np.vstack((targets_dx, targets_dy, targets_dw, targets_dh)).T

        return targets


def joint_stracks(tlista, tlistb):
    exists = {}
    res = []
    for t in tlista:
        exists[t.track_id] = 1
        res.append(t)
    for t in tlistb:
        tid = t.track_id
        if not exists.get(tid, 0):
            exists[tid] = 1
            res.append(t)
    return res


def sub_stracks(tlista, tlistb):
    stracks = {}
    for t in tlista:
        stracks[t.track_id] = t
    for t in tlistb:
        tid = t.track_id
        if stracks.get(tid, 0):
            del stracks[tid]
    return list(stracks.values())


def remove_duplicate_stracks(stracksa, stracksb):
    pdist = matching.iou_distance(stracksa, stracksb)
    pairs = np.where(pdist < 0.15)
    dupa, dupb = list(), list()
    for p, q in zip(*pairs):
        timep = stracksa[p].frame_id - stracksa[p].start_frame
        timeq = stracksb[q].frame_id - stracksb[q].start_frame
        if timep > timeq:
            dupb.append(q)
        else:
            dupa.append(p)
    resa = [t for i, t in enumerate(stracksa) if not i in dupa]
    resb = [t for i, t in enumerate(stracksb) if not i in dupb]
    return resa, resb


def save(obj, name):
    with open(f'/home/derry/Desktop/{name}.pth', 'wb') as f:
        pickle.dump(obj, f)


def load(name):
    with open(f'/home/derry/Desktop/{name}.pth', 'rb') as f:
        obj = pickle.load(f)
    return obj
