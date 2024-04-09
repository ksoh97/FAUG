from typing import Iterable
import matplotlib.pyplot as plt
import numpy as np
import torch
import util.misc as utils
import functools
from tqdm import tqdm
import torch.nn.functional as F
from monai.metrics import compute_meandice
from torch.autograd import Variable
from torchvision.transforms import GaussianBlur
import dataloaders.aggregative_fusion as af
import matplotlib.image
import os, sys
print = functools.partial(print, flush=True)

def train_warm_up(model: torch.nn.Module, criterion: torch.nn.Module,
                    data_loader: Iterable, optimizer: torch.optim.Optimizer,
                    device: torch.device, learning_rate:float, warmup_iteration: int = 1500):
    model.train()
    criterion.train()

    metric_logger = utils.MetricLogger(delimiter="  ")
    metric_logger.add_meter('lr', utils.SmoothedValue(window_size=1, fmt='{value:.6f}'))

    print_freq = 10
    cur_iteration=0
    while True:
        for i, samples in enumerate(metric_logger.log_every(data_loader, print_freq, 'WarmUp with max iteration: {}'.format(warmup_iteration))):
            for k,v in samples.items():
                if isinstance(samples[k],torch.Tensor):
                    samples[k]=v.to(device)
            cur_iteration+=1
            for i, param_group in enumerate(optimizer.param_groups):
                param_group["lr"] = cur_iteration/warmup_iteration*learning_rate * param_group["lr_scale"]

            img=samples['images']
            lbl=samples['labels']
            pred = model(img)
            loss_dict = criterion.get_loss(pred,lbl)
            losses = sum(loss_dict[k] * criterion.weight_dict[k] for k in loss_dict.keys())
            optimizer.zero_grad()
            losses.backward()
            optimizer.step()

            metric_logger.update(**loss_dict)
            metric_logger.update(lr=optimizer.param_groups[0]["lr"])
            if cur_iteration>=warmup_iteration:
                print(f'WarnUp End with Iteration {cur_iteration} and current lr is {optimizer.param_groups[0]["lr"]}.')
                return cur_iteration
        metric_logger.synchronize_between_processes()


def train_one_epoch(model: torch.nn.Module, criterion: torch.nn.Module,
                    data_loader: Iterable, optimizer: torch.optim.Optimizer,
                    device: torch.device, epoch: int, cur_iteration:int, max_iteration: int = -1, grad_scaler=None):
    model.train()
    criterion.train()

    metric_logger = utils.MetricLogger(delimiter="  ")
    metric_logger.add_meter('lr', utils.SmoothedValue(window_size=1, fmt='{value:.6f}'))

    header = 'Epoch: [{}]'.format(epoch)
    print_freq = 10

    for i, samples in enumerate(metric_logger.log_every(data_loader, print_freq, header)):
        for k, v in samples.items():
            if isinstance(samples[k], torch.Tensor):
                samples[k] = v.to(device)

        img = samples['images']
        lbl = samples['labels']

        if grad_scaler is None:
            pred = model(img)
            loss_dict = criterion.get_loss(pred,lbl)
            losses = sum(loss_dict[k] * criterion.weight_dict[k] for k in loss_dict.keys())
            optimizer.zero_grad()
            losses.backward()
            optimizer.step()
        else:
            with torch.cuda.amp.autocast():
                pred = model(img)
                loss_dict = criterion.get_loss(pred,lbl)
                losses = sum(loss_dict[k] * criterion.weight_dict[k] for k in loss_dict.keys())
            optimizer.zero_grad()
            grad_scaler.scale(losses).backward()
            grad_scaler.step(optimizer)
            grad_scaler.update()

        metric_logger.update(**loss_dict)
        metric_logger.update(lr=optimizer.param_groups[0]["lr"])
        cur_iteration+=1
        if cur_iteration>=max_iteration and max_iteration>0:
            break

    metric_logger.synchronize_between_processes()
    print("Averaged stats:", metric_logger)
    return cur_iteration


def train_one_epoch_FIESTA(model: torch.nn.Module, criterion: torch.nn.Module,
                    data_loader: Iterable, optimizer: torch.optim.Optimizer,
                    device: torch.device, epoch: int, cur_iteration:int, max_iteration: int = -1,visdir=None):
    model.train()
    criterion.train()

    metric_logger = utils.MetricLogger(delimiter="  ")
    metric_logger.add_meter('lr', utils.SmoothedValue(window_size=1, fmt='{value:.6f}'))

    header = 'Epoch: [{}]'.format(epoch)
    print_freq = 10
    visual_freq = 200  # 원래 500
    for i, samples in enumerate(metric_logger.log_every(data_loader, print_freq, header)):
        for k, v in samples.items():
            if isinstance(samples[k], torch.Tensor):
                samples[k] = v.to(device)

        CFAT_img = samples['images']  # [32, 1, 192, 192]
        LFAT_img = samples['aug_images']  # [32, 1, 192, 192]
        lbl = samples['labels']  # [32, 192, 192]
        if cur_iteration % visual_freq == 0:
            visual_dict={}
            visual_dict['CFAT']=CFAT_img.detach().cpu().numpy()[0,0]
            visual_dict['LFAT']=LFAT_img.detach().cpu().numpy()[0,0]
            visual_dict['GT']=lbl.detach().cpu().numpy()[0]
        else:
            visual_dict=None

        input_var = Variable(CFAT_img, requires_grad=True)

        optimizer.zero_grad()
        logits = model(input_var)
        loss_dict = criterion.get_loss(logits, lbl)
        losses = sum(loss_dict[k] * criterion.weight_dict[k] for k in loss_dict.keys() if k in criterion.weight_dict)
        losses.backward(retain_graph=True)

        # Uncertainty Guidance (CFAT-UG)
        proba = torch.nn.Softmax(dim=1)(model(input_var).detach())
        uncertainty = -torch.mean(proba*torch.log2(proba), 1)
        uncertainty_CFAT = af.rescale_intensity(torch.unsqueeze(uncertainty, 1))

        if visual_dict is not None:
            visual_dict['CFAT_pred'] = torch.argmax(logits,1).cpu().numpy()[0]

        if visual_dict is not None:
            visual_dict['uncertainty_map_CFAT'] = uncertainty_CFAT.detach().cpu().numpy()[0,0]

        aug_var = Variable(LFAT_img, requires_grad=True)
        aug_logits = model(aug_var)
        aug_loss_dict = criterion.get_loss(aug_logits, lbl)
        aug_losses = sum(aug_loss_dict[k] * criterion.weight_dict[k] for k in aug_loss_dict.keys() if k in criterion.weight_dict)
        aug_losses.backward(retain_graph=True)

        # Uncertainty Guidance (LFAT-UG)
        proba = torch.nn.Softmax(dim=1)(model(aug_var).detach())
        uncertainty = -torch.mean(proba*torch.log2(proba), 1)
        uncertainty_LFAT = af.rescale_intensity(torch.unsqueeze(uncertainty, 1))

        if visual_dict is not None:
            visual_dict['LFAT_pred'] = torch.argmax(aug_logits, 1).cpu().numpy()[0]

        if visual_dict is not None:
            visual_dict['uncertainty_map_LFAT'] = uncertainty_LFAT.detach().cpu().numpy()[0,0]

        # UG
        mean_UG = (uncertainty_CFAT + uncertainty_LFAT) / 2
        max_UG = torch.maximum(uncertainty_CFAT, uncertainty_LFAT)
        UG = (mean_UG + max_UG) / 2
        UG = af.rescale_intensity(GaussianBlur(kernel_size=(29, 29), sigma=(10, 10))(UG))

        gt = lbl.detach().cpu().numpy()
        gt = np.where(gt > 1, 1, gt)
        gt_ug = gt * UG[:, 0].detach().cpu().numpy()

        gt_idx_len = len(np.where(gt == 1)[0])
        uncer_sum = np.sum(gt_ug)

        if (uncer_sum / gt_idx_len) < 0.5:
            mixed_var = CFAT_img.detach() * UG + LFAT_img * (1 - UG)
        else:
            mixed_var = CFAT_img.detach() + LFAT_img

        mixed_var = Variable(mixed_var, requires_grad=True)
        mixed_logits = model(mixed_var)
        mixed_loss_dict = criterion.get_loss(mixed_logits, lbl)
        mix_losses = sum(mixed_loss_dict[k] * criterion.weight_dict[k] for k in mixed_loss_dict.keys() if k in criterion.weight_dict)
        mix_losses.backward()

        if visual_dict is not None:
            visual_dict['mixed_img'] = mixed_var.detach().cpu().numpy()[0,0]

        if visual_dict is not None:
            visual_dict['mixed_pred'] = torch.argmax(mixed_logits, 1).cpu().numpy()[0]

        if visual_dict is not None:
            visual_dict['combined-UG'] = UG.detach().cpu().numpy()[0,0]

        optimizer.step()

        all_loss_dict={}
        for k in loss_dict.keys():
            if k not in criterion.weight_dict:continue
            all_loss_dict[k]=loss_dict[k]
            all_loss_dict[k+'_aug']=aug_loss_dict[k]
            all_loss_dict[k+'_mix']=mixed_loss_dict[k]

        metric_logger.update(**all_loss_dict)
        metric_logger.update(lr=optimizer.param_groups[0]["lr"])


        if cur_iteration>=max_iteration and max_iteration>0:
            break

        if visdir is not None and cur_iteration%visual_freq==0:
            cur_iter_path = os.path.join(visdir, f"{cur_iteration}")  # uncertainty 시각화를 위해 저장
            if not os.path.exists(cur_iter_path): os.mkdir(cur_iter_path)  # uncertainty 시각화를 위해 저장
            fs=int(len(visual_dict)**0.5)+1
            for idx, k in enumerate(visual_dict.keys()):
                np.save(cur_iter_path + f"/{k}.npy", visual_dict[k])  # uncertainty 시각화를 위해 저장
                plt.subplot(fs,fs,idx+1)
                plt.title(k)
                plt.axis('off')
                if k not in ['GT', 'CFAT_pred',  'LFAT_pred', 'mixed_pred', 'mixed_pred']:
                    plt.imshow(visual_dict[k], cmap='gray')
                else:
                    plt.imshow(visual_dict[k], vmin=0, vmax=4)
            plt.tight_layout()
            plt.savefig(f'{visdir}/{cur_iteration}.png')
            plt.close()
        cur_iteration+=1

    metric_logger.synchronize_between_processes()
    print("Averaged stats:", metric_logger)
    return cur_iteration, aug_loss_dict

@torch.no_grad()
def evaluate(model: torch.nn.Module, data_loader: Iterable, device: torch.device):
    model.eval()
    def convert_to_one_hot(tensor,num_c):
        return F.one_hot(tensor,num_c).permute((0,3,1,2))
    dices=[]
    for samples in data_loader:
        for k, v in samples.items():
            if isinstance(samples[k], torch.Tensor):
                samples[k] = v.to(device)
        img = samples['images']
        lbl = samples['labels']
        logits = model(img)
        num_classes=logits.size(1)
        pred=torch.argmax(logits,dim=1)
        one_hot_pred=convert_to_one_hot(pred,num_classes)
        one_hot_gt=convert_to_one_hot(lbl,num_classes)
        dice=compute_meandice(one_hot_pred,one_hot_gt,include_background=False)
        dices.append(dice.cpu().numpy())
    dices=np.concatenate(dices,0)
    dices=np.nanmean(dices,0)
    return dices

def prediction_wrapper(model, test_loader, epoch, label_name, path, mode = 'base', save_prediction = False):
    """
    A wrapper for the ease of evaluation
    Args:
        model:          Module The network to evalute on
        test_loader:    DataLoader Dataloader for the dataset to test
        mode:           str Adding a note for the saved testing results
    """
    vis_save = os.path.join(path, "vis_save")
    if not os.path.exists(vis_save): os.makedirs(vis_save, exist_ok=True)

    model.eval()
    with torch.no_grad():
        out_prediction_list = {} # a buffer for saving results
        # recomp_img_list = []
        for idx, batch in tqdm(enumerate(test_loader), total = len(test_loader)):
            if batch['is_start']:
                slice_idx = 0

                scan_id_full = str(batch['scan_id'][0])
                out_prediction_list[scan_id_full] = {}

                nframe = batch['nframe']
                nb, nc, nx, ny = batch['images'].shape
                curr_pred = torch.Tensor(np.zeros( [ nframe,  nx, ny]  )).cuda() # nb/nz, nc, nx, ny
                curr_gth = torch.Tensor(np.zeros( [nframe,  nx, ny]  )).cuda()
                curr_img = np.zeros( [nx, ny, nframe]  )

            assert batch['labels'].shape[0] == 1 # enforce a batchsize of 1

            img = batch['images'].cuda()
            gth = batch['labels'].cuda()

            pred = model(img)
            pred=torch.argmax(pred,1)
            curr_pred[slice_idx, ...]   = pred[0, ...] # nb (1), nc, nx, ny
            curr_gth[slice_idx, ...]    = gth[0, ...]
            curr_img[:,:,slice_idx] = batch['images'][0, 0,...].numpy()
            slice_idx += 1

            if batch['is_end']:
                out_prediction_list[scan_id_full]['pred'] = curr_pred
                out_prediction_list[scan_id_full]['gth'] = curr_gth
                # if opt.phase == 'test':
                #     recomp_img_list.append(curr_img)

            f_num = nframe.detach().cpu().numpy()
            matplotlib.image.imsave(vis_save + f'/img_{f_num[0]}_{slice_idx}.png', img[0,0].detach().cpu().numpy())
            matplotlib.image.imsave(vis_save + f'/gt_{f_num[0]}_{slice_idx}.png', gth[0].detach().cpu().numpy())
            matplotlib.image.imsave(vis_save + f'/pred_{f_num[0]}_{slice_idx}.png', pred[0].detach().cpu().numpy())

        print("Epoch {} test result on mode {} segmentation are shown as follows:".format(epoch, mode))
        error_dict, dsc_table, domain_names = eval_list_wrapper(out_prediction_list, len(label_name),label_name)
        error_dict["mode"] = mode
        if not save_prediction: # to save memory
            del out_prediction_list
            out_prediction_list = []
        torch.cuda.empty_cache()

    return out_prediction_list, dsc_table, error_dict, domain_names

def eval_list_wrapper(vol_list, nclass, label_name):
    """
    Evaluatation and arrange predictions
    """
    def convert_to_one_hot2(tensor,num_c):
        return F.one_hot(tensor.long(),num_c).permute((3,0,1,2)).unsqueeze(0)

    out_count = len(vol_list)
    tables_by_domain = {} # tables by domain
    dsc_table = np.ones([ out_count, nclass ]  ) # rows and samples, columns are structures
    idx = 0
    for scan_id, comp in vol_list.items():
        domain, pid = scan_id.split("_")
        if domain not in tables_by_domain.keys():
            tables_by_domain[domain] = {'scores': [],'scan_ids': []}
        pred_ = comp['pred']
        gth_  = comp['gth']
        dices=compute_meandice(y_pred=convert_to_one_hot2(pred_,nclass),y=convert_to_one_hot2(gth_,nclass),include_background=True).cpu().numpy()[0].tolist()

        tables_by_domain[domain]['scores'].append( [_sc for _sc in dices]  )
        tables_by_domain[domain]['scan_ids'].append( scan_id )
        dsc_table[idx, ...] = np.reshape(dices, (-1))
        del pred_
        del gth_
        idx += 1
        torch.cuda.empty_cache()

    # then output the result
    error_dict = {}
    for organ in range(nclass):
        mean_dc = np.mean( dsc_table[:, organ] )
        std_dc  = np.std(  dsc_table[:, organ] )
        print("Organ {} with dice: mean: {:06.5f}, std: {:06.5f}".format(label_name[organ], mean_dc, std_dc))
        error_dict[label_name[organ]] = mean_dc
    print("Overall std dice by sample {:06.5f}".format(dsc_table[:, 1:].std()))
    print("Overall mean dice by sample {:06.5f}".format( dsc_table[:,1:].mean())) # background is noted as class 0 and therefore not counted
    error_dict['overall'] = dsc_table[:,1:].mean()

    # then deal with table_by_domain issue
    overall_by_domain = []
    domain_names = []
    for domain_name, domain_dict in tables_by_domain.items():
        domain_scores = np.array( tables_by_domain[domain_name]['scores']  )
        domain_mean_score = np.mean(domain_scores[:, 1:])
        error_dict[f'domain_{domain_name}_overall'] = domain_mean_score
        error_dict[f'domain_{domain_name}_table'] = domain_scores
        overall_by_domain.append(domain_mean_score)
        domain_names.append(domain_name)
    print('per domain resutls:', overall_by_domain)
    error_dict['overall_by_domain'] = np.mean(overall_by_domain)

    print("Overall mean dice by domain {:06.5f}".format( error_dict['overall_by_domain'] ) )
    return error_dict, dsc_table, domain_names

