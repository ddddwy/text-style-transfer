import sys

import json
import data
import models
import utils
import numpy as np
import logging
import argparse
import os
import time
import glob

import torch
import torch.nn as nn
import torch.optim as optim
import tensorflow as tf

import evaluation
from cuda import CUDA


parser = argparse.ArgumentParser()
parser.add_argument("--config", help="path to json config", required=True)
parser.add_argument("--bleu", help="do BLEU eval", action='store_true')
parser.add_argument("--overfit", help="train continuously on one batch of data", action='store_true')

args = parser.parse_args()
config = json.load(open(args.config, 'r'))
working_dir = config['data']['working_dir']

if not os.path.exists(working_dir):
    os.makedirs(working_dir)

config_path = os.path.join(working_dir, 'config.json')
if not os.path.exists(config_path):
    with open(config_path, 'w') as f:
        json.dump(config, f)

# set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    filename='%s/train_log' % working_dir,
)

console = logging.StreamHandler()
console.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
console.setFormatter(formatter)
logging.getLogger('').addHandler(console)

logging.info('Reading data ...')
# train time: just pick attributes that are close to the current (using word distance)
src = data.gen_train_data(src=config['data']['src'], config=config, attribute_vocab=config['data']['attribute_vocab'])
# dev time: scan through train content (using tfidf) and retrieve corresponding attributes
src_dev, tgt_dev = data.gen_dev_data(src=config['data']['src_dev'], config=config, tgt=config['data']['tgt_dev'],
                                     attribute_vocab=config['data']['attribute_vocab'], train_src=config['data']['src_dev'])
logging.info('...done!')


batch_size = config['data']['batch_size']
max_length = config['data']['max_len']
src_vocab_size = len(src['tok2id'])


weight_mask = torch.ones(src_vocab_size)
weight_mask[src['tok2id']['<pad>']] = 0
loss_criterion = nn.CrossEntropyLoss(weight=weight_mask)
if CUDA:
    weight_mask = weight_mask.cuda()
    loss_criterion = loss_criterion.cuda()

# ensure that the parameter initialization values are the same every time we strat training
torch.manual_seed(config['training']['random_seed'])
np.random.seed(config['training']['random_seed'])

model = models.SeqModel(src_vocab_size=src_vocab_size, tgt_vocab_size=src_vocab_size,
                        pad_id_src=src['tok2id']['<pad>'], pad_id_tgt=src['tok2id']['<pad>'],
                        config=config)
logging.info('MODEL HAS %s params' %  model.count_params())

# get most recent checkpoint
model, start_epoch = models.attempt_load_model(model=model, checkpoint_dir=working_dir)
if CUDA:
    model = model.cuda()

writer = tf.summary.FileWriter(working_dir)

if config['training']['optimizer'] == 'adam':
    lr = config['training']['learning_rate']
    optimizer = optim.Adam(model.parameters(), lr=lr)
elif config['training']['optimizer'] == 'sgd':
    lr = config['training']['learning_rate']
    optimizer = optim.SGD(model.parameters(), lr=lr)
elif config['training']['optimizer']=='adadelta':
    lr = config['training']['learning_rate']
    optimizer = optim.Adadelta(model.parameters(), lr=lr)
else:
    raise NotImplementedError("Learning method not recommend for task")

epoch_loss = []
start_since_last_report = time.time()
words_since_last_report = 0
losses_since_last_report = []
best_metric = 0.0
best_epoch = 0
cur_metric = 0.0 # log perplexity or BLEU
num_batches = len(src['content']) // batch_size
with open(working_dir + '/stats_labels.csv', 'w') as f:
    f.write(utils.config_key_string(config) + ',%s,%s\n' % (('bleu' if args.bleu else 'dev_loss'), 'best_epoch'))

STEP = 0
for epoch in range(start_epoch, config['training']['epochs']):
    # if epoch > 3 and cur_metric == 0 or epoch > 7 and cur_metric < 10 or epoch > 15 and cur_metric < 15:
    #     logging.info('QUITTING...NOT LEARNING WELL')
    #     with open(working_dir + '/stats.csv', 'w') as f:
    #         f.write(utils.config_val_string(config) + ',%s,%s\n' % (
    #             best_metric, best_epoch))
    #     break

    if cur_metric > best_metric:
        # rm old checkpoint
        for ckpt_path in glob.glob(working_dir + '/model.*'):
            os.system("rm %s" % ckpt_path)
        # replace with new checkpoint
        torch.save(model.state_dict(), working_dir + '/model.%s.ckpt' % epoch)

        best_metric = cur_metric
        best_epoch = epoch - 1

    losses = []
    for i in range(0, len(src['content']), batch_size):
        if args.overfit:
            i = batch_size

        batch_idx = i / batch_size

        input_content, input_aux, output = data.minibatch(src, src, i, batch_size, max_length, 
                                                          config['model']['model_type'])
        input_content_src, _, srclens, srcmask, _ = input_content
        input_ids_aux, _, auxlens, auxmask, _ = input_aux
        input_data_tgt, output_lines_tgt, _, _, _ = output
        
        decoder_logit, decoder_probs = model(input_content_src, input_data_tgt, srcmask, srclens,
                                             input_ids_aux, auxlens, auxmask)

        optimizer.zero_grad()
        loss = loss_criterion(decoder_logit.contiguous().view(-1, src_vocab_size),
                              output_lines_tgt.view(-1))
        losses.append(loss.item())
        losses_since_last_report.append(loss.item())
        epoch_loss.append(loss.item())
        loss.backward()
        
        norm = nn.utils.clip_grad_norm_(model.parameters(), config['training']['max_norm'])
        tf.summary.scalar('grad_norm', norm)
        optimizer.step()

        if args.overfit or batch_idx % config['training']['batches_per_report'] == 0:
            s = float(time.time() - start_since_last_report)
            wps = (batch_size * config['training']['batches_per_report']) / s
            avg_loss = np.mean(losses_since_last_report)
            info = (epoch, batch_idx, num_batches, wps, avg_loss, cur_metric)
            tf.summary.scalar('WPS', wps)
            tf.summary.scalar('avg_loss', avg_loss)
            logging.info('EPOCH: %s ITER: %s/%s WPS: %.2f LOSS: %.4f METRIC: %.4f' % info)
            start_since_last_report = time.time()
            words_since_last_report = 0
            losses_since_last_report = []

        # NO SAMPLING!! because weird train-vs-test data stuff would be a pain
        STEP += 1
    if args.overfit:
        continue

    logging.info('EPOCH %s COMPLETE. EVALUATING...' % epoch)
    start = time.time()
    model.eval()
    dev_loss = evaluation.evaluate_lpp_val(model=model, src=tgt_dev, tgt=src_dev, config=config)
    tf.summary.scalar('dev_loss', dev_loss)

    if args.bleu and epoch >= config['training'].get('bleu_start_epoch', 1):
        cur_metric, edit_distance, precision, recall, inputs, preds, golds, auxs = evaluation.inference_metrics(
            model, src_dev, tgt_dev, config)

        with open(working_dir + '/auxs.%s' % epoch, 'w') as f:
            f.write('\n'.join(auxs) + '\n')
        with open(working_dir + '/inputs.%s' % epoch, 'w') as f:
            f.write('\n'.join(inputs) + '\n')
        with open(working_dir + '/preds.%s' % epoch, 'w') as f:
            f.write('\n'.join(preds) + '\n')
        with open(working_dir + '/golds.%s' % epoch, 'w') as f:
            f.write('\n'.join(golds) + '\n')

        tf.summary.scalar('eval_precision', precision)
        tf.summary.scalar('eval_recall', recall)
        tf.summary.scalar('eval_edit_distance', edit_distance)
        tf.summary.scalar('eval_bleu', cur_metric)
    else:
        cur_metric = dev_loss

    model.train()

    logging.info('METRIC: %s. TIME: %.2fs CHECKPOINTING...' % (cur_metric, (time.time() - start)))
    avg_loss = np.mean(epoch_loss)
    epoch_loss = []

#    merge_summary = tf.summary.merge_all() 
#    writer.add_summary(merge_summary, epoch)
    
    
with open(working_dir + '/stats.csv', 'w') as f:
    f.write(utils.config_val_string(config) + ',%s,%s\n' % (best_metric, best_epoch))

