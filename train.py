import argparse
import sys
import os
import shutil
import time
import logging
import datetime
import json

import torch
import torch.nn as nn
import torch.backends.cudnn as cudnn
import torch.optim
import torch.utils.data
import progressbar
import numpy as np
from torch.autograd import Variable
from visdom import Visdom

import models
from dataset import VQADataset
from eval_tools import get_eval
from config import cfg, cfg_from_file, cfg_from_list


parser = argparse.ArgumentParser(description='Train VQA model')
parser.add_argument('-j', '--workers', default=2, type=int, metavar='N',
                    help='number of data loading workers (default: 2)')
parser.add_argument('--epochs', default=200, type=int, metavar='N',
                    help='number of total epochs to run')
parser.add_argument('--start-epoch', default=0, type=int, metavar='N',
                    help='manual epoch number (useful on restarts)')
parser.add_argument('--print-freq', '-p', default=10, type=int,
                    metavar='N', help='print frequency (default: 10)')
parser.add_argument('--model', '-m', default='Baseline',
                    help='name of the model')
parser.add_argument('--gpu_id', default=0, type=int, metavar='N',
                    help='index of the gpu')
parser.add_argument('--lr-decay-start', default=40, metavar='N',
                    help='epoch number starting decay learning rate')
parser.add_argument('--lr-decay-factor', default=0.8, metavar='FLOAT',
                    help='learning rate decay factor for every 10 epochs')
parser.add_argument('--cfg', dest='cfg_file', default=None, type=str,
                    help='optional config file')
parser.add_argument('--set', dest='set_cfgs', default=None,
                    nargs=argparse.REMAINDER, help='set config keys')

if cfg.USE_RANDOM_SEED:
    torch.manual_seed(cfg.SEED)
    torch.cuda.manual_seed(cfg.SEED)

logger = logging.getLogger('vqa')
logger.setLevel(logging.DEBUG)


def main():
    global args
    args = parser.parse_args()
    args_str = json.dumps(vars(args), indent=2)

    if args.cfg_file is not None:
        cfg_from_file(args.cfg_file)
    if args.set_cfgs is not None:
        cfg_from_list(args.set_cfgs)

    # use timestamp as log subdirectory
    timestamp = datetime.datetime.now().strftime('%Y%m%d%H%M%S')
    cfg.LOG_DIR= os.path.join(cfg.LOG_DIR, timestamp)
    os.mkdir(cfg.LOG_DIR)
    json.dump(cfg, open(cfg.LOG_DIR + '/config.json', 'w'), indent=2)
    shutil.copy('models.py', cfg.LOG_DIR)

    # init ploter
    ploter = Ploter(timestamp)

    # setting log handlers
    fh = logging.FileHandler(os.path.join(cfg.LOG_DIR, 'log'))
    fh.setLevel(logging.DEBUG)
    fhc = logging.FileHandler('current.log')
    fhc.setLevel(logging.DEBUG)
    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.DEBUG)

    fmt = '[%(asctime)-15s] %(message)s'
    datefmt = '%Y-%m-%d %H:%M:%S'
    formatter = logging.Formatter(fmt, datefmt)

    fh.setFormatter(formatter)
    fhc.setFormatter(formatter)
    logger.addHandler(fh)
    logger.addHandler(fhc)
    logger.addHandler(sh)
    logger.debug('[Info] called with: ' + args_str)

    logger.debug('[Info] timestamp: ' + timestamp)

    # select device
    torch.cuda.set_device(args.gpu_id)
    logger.debug('[Info] use gpu: {}'.format(torch.cuda.current_device()))

    # data
    logger.debug('[Info] init dataset')
    needT = args.model not in ('V2V', 'MultiAttModel')
    trn_set = VQADataset('train', needT)
    val_set = VQADataset('test', needT)

    train_loader = torch.utils.data.DataLoader(
            trn_set, batch_size=cfg.BATCH_SIZE, shuffle=True,
            num_workers=args.workers, pin_memory=True)
    val_loader = torch.utils.data.DataLoader(
            val_set, batch_size=cfg.BATCH_SIZE, shuffle=False,
            num_workers=args.workers, pin_memory=True)

    # model
    emb_size = 300
    if cfg.WORD_EMBEDDINGS:
        word_vec = merge_embeddings(cfg.WORD_EMBEDDINGS)
        aword = next(iter(word_vec))
        emb_size = len(word_vec[aword])
        logger.debug('[Info] embedding size: {}'.format(emb_size))

    logger.debug('[Info] construct model, criterion and optimizer')
    model = getattr(models, args.model)(
            num_words=trn_set.num_words,
            num_ans=trn_set.num_ans,
            emb_size=emb_size)
    logger.debug('[Info] model name: ' + args.model)

    # initialize word embedding with pretrained
    if cfg.WORD_EMBEDDINGS:
        emb = model.we.weight.data.numpy()
        words = trn_set.codebook['itow']
        assert '<PAD>' not in word_vec
        for i, w in enumerate(words):
            if w in word_vec:
                emb[i] = word_vec[w]
        model.we.weight = nn.Parameter(torch.from_numpy(emb))

    model.cuda()

    criterion = nn.CrossEntropyLoss().cuda()
    optimizer = torch.optim.RMSprop(model.parameters(), cfg.LEARNING_RATE,
                                    weight_decay=cfg.WEIGHT_DECAY)
    cudnn.benchmark = True


    # train
    logger.debug('[Info] start training...')
    best_acc = 0
    best_epoch = -1
    for epoch in range(args.start_epoch, args.epochs):
        lr = adjust_learning_rate(optimizer, epoch)

        loss = train(train_loader, model, criterion, optimizer, epoch)
        acc = validate(val_loader, model, criterion, epoch)

        ploter.append(epoch, loss, 'train-loss')
        ploter.append(epoch, acc, 'val-acc')
        ploter.append(epoch, lr, 'lr')

        if acc > best_acc:
            is_best = True
            best_acc = acc
            best_epoch = epoch

        logger.debug('Evaluate Result:\t'
                     'Acc  {0}\t'
                     'Best {1} ({2})'.format(acc, best_acc, best_epoch))

        # save checkpoint
        cp_fname = 'checkpoint-{:03}.pth.tar'.format(epoch)
        cp_path = os.path.join(cfg.LOG_DIR, cp_fname)
        state = {
            'epoch': epoch + 1,
            'state_dict': model.state_dict(),
            'best_acc': best_acc,
            'optimizer': optimizer.state_dict()
            }
        torch.save(state, cp_path)
        if is_best:
            best_path = os.path.join(cfg.LOG_DIR, 'model-best.pth.tar')
            shutil.copyfile(cp_path, best_path)


def merge_embeddings(embedding_names):
    names = embedding_names.split('+')
    name = names[0]
    vocab, vecs = load_embeddings(name)
    if len(names) > 1:
        vecs_list = [vecs]
        for name in names[1:]:
            _, vecs = load_embeddings(name)
            # the order of vocab in glove is the same
            vecs_list.append(vecs)
            vecs = np.hstack(vecs_list)
    return dict(zip(vocab, vecs))


def load_embeddings(name):
    emb_path = '{}/word-embedding/{}'.format(cfg.DATA_DIR, name)
    logger.debug('[Load] ' + emb_path)
    with open(emb_path) as f:
        word_vec_txt = [l.strip().split(' ', 1) for l in f.readlines()]
    vocab, vecs_txt = zip(*word_vec_txt)
    # infer vector dimention
    vec_size = len(vecs_txt[0].split())
    # fromstring faster than loadtxt
    vecs = np.fromstring(' '.join(vecs_txt), dtype='float32', sep=' ')
    vecs = vecs.reshape(-1, vec_size)
    return vocab, vecs


def train(train_loader, model, criterion, optimizer, epoch):
    batch_time = AverageMeter()
    data_time = AverageMeter()
    losses = AverageMeter()

    model.train()

    end = time.time()
    # sample: (que_id, img, que, [obj], ans)
    for i, sample in enumerate(train_loader):
        data_time.update(time.time() - end)

        sample_var = [Variable(d).cuda() for d in list(sample)[1:]]

        score = model(*sample_var[:-1])
        loss = criterion(score, sample_var[-1])

        losses.update(loss.data[0], sample[0].size(0))

        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        batch_time.update(time.time() - end)
        end = time.time()

        if i % args.print_freq == 0:
            logger.debug(
                'Epoch: [{0}][{1}/{2}]\t'
                'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                'Data {data_time.val:.3f} ({data_time.avg:.3f})\t'
                'Loss {loss.val:.4f} ({loss.avg:.4f})'.format(
                 epoch, i, len(train_loader), batch_time=batch_time,
                 data_time=data_time, loss=losses))
    return losses.avg


def validate(val_loader, model, criterion, epoch):
    model.eval()
    itoa = val_loader.dataset.codebook['itoa']

    results = []
    end = time.time()
    bar = progressbar.ProgressBar()
    # sample: (que_id, img, que, [obj])
    for sample in bar(val_loader):
        sample_var = [Variable(d).cuda() for d in list(sample)[1:]]

        score = model(*sample_var)

        results.extend(format_result(sample[0], score, itoa))

    vqa_eval = get_eval(results, 'val2014')

    # save result and accuracy
    result_file = os.path.join(cfg.LOG_DIR,
                               'result-{:03}.json'.format(epoch))
    json.dump(results, open(result_file, 'w'))
    acc_file = os.path.join(cfg.LOG_DIR,
                            'accuracy-{:03}.json'.format(epoch))
    json.dump(vqa_eval.accuracy, open(acc_file, 'w'))

    return vqa_eval.accuracy['overall']


def format_result(que_ids, scores, itoa):
    _, ans_ids = torch.max(scores.data, dim=1)

    result = []
    for que_id, ans_id in zip(que_ids, ans_ids):
        result.append({'question_id': que_id,
                       'answer': itoa[ans_id]})
    return result


def save_checkpoint(state, is_best, filename='checkpoint.pth.tar'):
    torch.save(state, filename)
    if is_best:
        shutil.copyfile(filename, 'model_best.pth.tar')


class AverageMeter(object):
    def __init__(self):
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count


def adjust_learning_rate(optimizer, epoch):
    exponent = max(0, (epoch - args.lr_decay_start) // 10 + 1)
    lr = cfg.LEARNING_RATE * (args.lr_decay_factor ** exponent)
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr
    return lr


class Ploter(object):
    def __init__(self, env_name):
        self.viz = Visdom(env=env_name)
        self.win = None

    def append(self, x, y, name):
        if self.win is not None:
            self.viz.updateTrace(
                    X=np.array([x]),
                    Y=np.array([y]),
                    win=self.win,
                    name=name)
        else:
            self.win = self.viz.line(
                    X=np.array([x]),
                    Y=np.array([y]),
                    opts={'legend': [name]})


if __name__ == '__main__':
    main()

