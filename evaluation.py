import math
import numpy as np
import sys
from collections import Counter

import torch
from torch.autograd import Variable
import torch.nn as nn
import editdistance

import data
from cuda import CUDA

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
# BLEU functions from https://github.com/MaximumEntropy/Seq2Seq-PyTorch
#    (ran some comparisons, and it matches moses's multi-bleu.perl)
def bleu_stats(hypothesis, reference):
    """Compute statistics for BLEU."""
    stats = []
    stats.append(len(hypothesis))
    stats.append(len(reference))
    for n in range(1, 5):
        s_ngrams = Counter(
            [tuple(hypothesis[i:i + n]) for i in range(len(hypothesis) + 1 - n)]
        )
        r_ngrams = Counter(
            [tuple(reference[i:i + n]) for i in range(len(reference) + 1 - n)]
        )
        stats.append(max([sum((s_ngrams & r_ngrams).values()), 0]))
        stats.append(max([len(hypothesis) + 1 - n, 0]))
    return stats

def bleu(stats):
    """Compute BLEU given n-gram statistics."""
    if len(list(filter(lambda x: x == 0, stats))) > 0:
        return 0
    (c, r) = stats[:2]
    log_bleu_prec = sum(
        [math.log(float(x) / y) for x, y in zip(stats[2::2], stats[3::2])]
    ) / 4.
    return math.exp(min([0, 1 - float(r) / c]) + log_bleu_prec)

def get_bleu(hypotheses, reference):
    """Get validation BLEU score for dev set."""
    stats = np.array([0., 0., 0., 0., 0., 0., 0., 0., 0., 0.])
    for hyp, ref in zip(hypotheses, reference):
        stats += np.array(bleu_stats(hyp, ref))
    return 100 * bleu(stats)
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #


def get_edit_distance(hypotheses, reference):
    ed = 0
    for hyp, ref in zip(hypotheses, reference):
        ed += editdistance.eval(hyp, ref)

    return ed * 1.0 / len(hypotheses)


def get_precisions_recalls(inputs, preds, ground_truths):
    def precision_recall(src, tgt, pred):
        src_set = set(src)
        tgt_set = set(tgt)
        pred_set = set(pred)
    
        tgt_unique = tgt_set - src_set
        src_unique = src_set - tgt_set
        shared = tgt_set & src_set
        
        correct_shared = len(pred_set & shared)
        correct_tgt = len(pred_set & tgt_unique)
        
        incorrect_src = len(pred_set & src_unique)
        incorrect_unseen = len(pred_set - src_set - tgt_set)
        
        # words the model correctly introduced
        tp = correct_tgt
        # words the model incorrectly introduced
        fp = incorrect_unseen
        # bias words the model incorrectly kept
        fn = incorrect_src
        
        precision = tp * 1.0 / (tp + fp + 0.001)
        recall = tp * 1.0 / (tp + fn + 0.001)

        return precision, recall

    [precisions, recalls] = list(zip(*[
        precision_recall(src, tgt, pred) 
        for src, tgt, pred in zip(inputs, ground_truths, preds)
    ]))

    return precisions, recalls



def decode_minibatch(max_len, start_id, model, src_input, srclens, srcmask,
        aux_input, auxlens, auxmask):
    """ argmax decoding """
    # Initialize target with <s> for every sentence
    tgt_input = Variable(torch.LongTensor([[start_id] for i in range(src_input.size(0))]))
    if CUDA:
        tgt_input = tgt_input.cuda()

    for i in range(max_len):
        # run input through the model
        decoder_logit, word_probs = model(src_input, tgt_input, srcmask, srclens,
            aux_input, auxmask, auxlens)
        decoder_argmax = word_probs.data.cpu().numpy().argmax(axis=-1)
        # select the predicted "next" tokens, attach to target-side inputs
        next_preds = Variable(torch.from_numpy(decoder_argmax[:, -1]))
        if CUDA:
            next_preds = next_preds.cuda()
        tgt_input = torch.cat((tgt_input, next_preds.unsqueeze(1)), dim=1)

    return tgt_input


def decode_dataset(model, src, tgt, config):
    """Evaluate model."""
    inputs = []
    preds = []
    auxs = []
    ground_truths = []
    for j in range(0, len(src['data']), config['data']['batch_size']):
        sys.stdout.write("\r%s/%s..." % (j, len(src['data'])))
        sys.stdout.flush()

        # get batch
        input_content, input_aux, output = data.minibatch(
            src, tgt, j, 
            config['data']['batch_size'], 
            config['data']['max_len'], 
            config['model']['model_type'],
            is_test=True)
        input_lines_src, output_lines_src, srclens, srcmask, indices = input_content
        input_ids_aux, _, auxlens, auxmask, _ = input_aux
        input_lines_tgt, output_lines_tgt, _, _, _ = output

        # TODO -- beam search
        tgt_pred = decode_minibatch(
            config['data']['max_len'], tgt['tok2id']['<s>'], 
            model, input_lines_src, srclens, srcmask,
            input_ids_aux, auxlens, auxmask)

        # convert seqs to tokens
        def ids_to_toks(tok_seqs, id2tok):
            out = []
            # take off the gpu
            tok_seqs = tok_seqs.cpu().numpy()
            # convert to toks, cut off at </s>, delete any start tokens (preds were kickstarted w them)
            for line in tok_seqs:
                toks = [id2tok[x] for x in line]
                if '<s>' in toks: 
                    toks.remove('<s>')
                cut_idx = toks.index('</s>') if '</s>' in toks else len(toks)
                out.append( toks[:cut_idx] )
            # unsort
            out = data.unsort(out, indices)
            return out

        # convert inputs/preds/targets/aux to human-readable form
        inputs += ids_to_toks(output_lines_src, src['id2tok'])
        preds += ids_to_toks(tgt_pred, tgt['id2tok'])
        ground_truths += ids_to_toks(output_lines_tgt, tgt['id2tok'])
        
        if config['model']['model_type'] == 'delete':
            auxs += [[str(x)] for x in input_ids_aux.data.cpu().numpy()] # because of list comp in inference_metrics()
        elif config['model']['model_type'] == 'delete_retrieve':
            auxs += ids_to_toks(input_ids_aux, tgt['id2tok'])
        elif config['model']['model_type'] == 'seq2seq':
            auxs += ['None' for _ in range(len(tgt_pred))]

    return inputs, preds, ground_truths, auxs


def inference_metrics(model, src, tgt, config):
    """ decode and evaluate bleu """
    inputs, preds, ground_truths, auxs = decode_dataset(
        model, src, tgt, config)

    bleu = get_bleu(preds, ground_truths)
    edit_distance = get_edit_distance(preds, ground_truths)
    precisions, recalls = get_precisions_recalls(inputs, preds, ground_truths)

    precision = np.average(precisions)
    recall = np.average(recalls)

    inputs = [' '.join(seq) for seq in inputs]
    preds = [' '.join(seq) for seq in preds]
    ground_truths = [' '.join(seq) for seq in ground_truths]
    auxs = [' '.join(seq) for seq in auxs]

    return bleu, edit_distance, precision, recall, inputs, preds, ground_truths, auxs


def evaluate_lpp(model, src, tgt, config):
    """ evaluate log perplexity WITHOUT decoding
        (i.e., with teacher forcing)
    """
    weight_mask = torch.ones(len(tgt['tok2id']))
    if CUDA:
        weight_mask = weight_mask.cuda()
    weight_mask[tgt['tok2id']['<pad>']] = 0
    loss_criterion = nn.CrossEntropyLoss(weight=weight_mask)
    if CUDA:
        loss_criterion = loss_criterion.cuda()

    losses = []
    for j in range(0, len(src['data']), config['data']['batch_size']):
        # get batch
        input_content, input_aux, output = data.minibatch(
            src, tgt, j, 
            config['data']['batch_size'], 
            config['data']['max_len'], 
            config['model']['model_type'],
            is_test=True)
        input_lines_src, _, srclens, srcmask, _ = input_content
        input_ids_aux, _, auxlens, auxmask, _ = input_aux
        input_lines_tgt, output_lines_tgt, _, _, _ = output

        decoder_logit, decoder_probs = model(
            input_lines_src, input_lines_tgt, srcmask, srclens,
            input_ids_aux, auxlens, auxmask)

        loss = loss_criterion(
            decoder_logit.contiguous().view(-1, len(tgt['tok2id'])),
            output_lines_tgt.view(-1)
        )
        losses.append(loss.item())

    return np.mean(losses)


def evaluate_lpp_val(model, src, tgt, config):
    """ 
    evaluate log perplexity WITHOUT decoding
    (i.e., with teacher forcing)
    
    args:
        src: src data object (i.e. data 0, learnt by the model)
        tgt: target data object (i.e. data 1, not learnt by the model)
    """
    weight_mask = torch.ones(len(tgt['tok2id']))
    if CUDA:
        weight_mask = weight_mask.cuda()
    weight_mask[tgt['tok2id']['<pad>']] = 0
    loss_criterion = nn.CrossEntropyLoss(weight=weight_mask)
    if CUDA:
        loss_criterion = loss_criterion.cuda()

    losses = []
    decoded_results = []
    for j in range(0, len(src['data']), config['data']['batch_size']):
        # batch_size = 1
        input_content, _, _ = data.minibatch(src, tgt, j, 1, config['data']['max_len'], 
                                                          config['model']['model_type'], is_test=True)
        input_content_src, _, _, _, content_idx = input_content
        
        tgt_dist_measurer = tgt['dist_measurer']
        related_content_tgt = tgt_dist_measurer.most_similar(content_idx[0])   # list of n seq_str
        # related_content_tgt = source_content_str, target_content_str, target_att_str, idx, score
        
        n_decoded_sents = []
        for i, single_data_tgt in enumerate(related_content_tgt):
            input_content_tgt, tgtlens, tgtmask = word2id(single_data_tgt[1], '<s>', tgt, config['data']['max_len'])
            input_content_tgt = Variable(torch.LongTensor(input_content_tgt))
            tgtlens = Variable(torch.LongTensor(tgtlens))
            tgtmask = Variable(torch.LongTensor(tgtmask))
            
            input_ids_aux, auxlens, auxmask = word2id(single_data_tgt[2], None, tgt, config['data']['max_len'])
            input_ids_aux = Variable(torch.LongTensor(input_ids_aux))
            auxlens = Variable(torch.LongTensor(auxlens))
            auxmask = Variable(torch.LongTensor(auxmask))
            
            output_data_tgt, _, _ = word2id(tgt['data'][single_data_tgt[3]], '</s>', tgt, config['data']['max_len'])
            output_data_tgt = Variable(torch.LongTensor(output_data_tgt))
            if CUDA:
                input_content_tgt = input_content_tgt.cuda()
                tgtlens = tgtlens.cuda()
                tgtmask = tgtmask.cuda()
                input_ids_aux = input_ids_aux.cuda()
                auxlens = auxlens.cuda()
                auxmask = auxmask.cuda()
                output_data_tgt = output_data_tgt.cuda()
            
            decoder_logit, decoder_probs = model(input_content_tgt, output_data_tgt, tgtmask, tgtlens, 
                                                 input_ids_aux, auxlens, auxmask)
            loss = loss_criterion(decoder_logit.contiguous().view(-1, len(tgt['tok2id'])), 
                                  output_data_tgt.view(-1))
            losses.append(loss.item())
            
            decoded_data_tgt = decode_minibatch(20, tgt['tok2id']['<s>'], 
                                                model, input_content_tgt, tgtlens, tgtmask,
                                                input_ids_aux, auxlens, auxmask)
            n_decoded_sents.append(id2word(decoded_data_tgt, tgt))
        decoded_results.append(n_decoded_sents)

    return np.mean(losses), decoded_results


def id2word(decoded_tensor, tgt):
    decoded_array = decoded_tensor.cpu().numpy()
    sent = []
    for i in range(len(decoded_array[0])):
        word = tgt['id2tok'][decoded_array[0, i]]
        sent.append(word)
        if word == '</s>':
            break
    return ' '.join(sent[1:-1])


def word2id(seq_str, tag, tgt, max_len):
    wid_list = []
    seq_len = 0
    mask = []
    if tag == '<s>':
        wid_list.append(tgt['tok2id']['<s>'])
        words = seq_str.strip().split()
        for word in words:
            if word in tgt['tok2id'].keys():
                wid = tgt['tok2id'][word]
            else:
                wid = tgt['tok2id']['<unk>']
            wid_list.append(wid)
        if len(wid_list) < max_len:
            seq_len = len(wid_list)
            wid_list += (max_len-len(wid_list))*[tgt['tok2id']['<pad>']]
            mask = [0]*seq_len + [1]*(max_len-seq_len)
        else:
            seq_len = max_len
            wid_list = wid_list[:max_len]
            mask = [0]*seq_len
    if tag == '</s>':
#        words = seq_str.strip().split()
        for word in seq_str:
            if word in tgt['tok2id'].keys():
                wid = tgt['tok2id'][word]
            else:
                wid = tgt['tok2id']['<unk>']
            wid_list.append(wid)
        wid_list.append(tgt['tok2id']['</s>'])
        if len(wid_list) < max_len:
            seq_len = len(wid_list)
            wid_list += (max_len-len(wid_list))*[tgt['tok2id']['<pad>']]
            mask = [0]*seq_len + [1]*(max_len-seq_len)
        else:
            seq_len = max_len
            wid_list = wid_list[:max_len]
            mask = [0]*seq_len
    if tag == None:
        words = seq_str.strip().split()
        for word in seq_str:
            if word in tgt['tok2id'].keys():
                wid = tgt['tok2id'][word]
            else:
                wid = 1
            wid_list.append(wid)
        if len(wid_list) < max_len:
            seq_len = len(wid_list)
            wid_list += (max_len-len(wid_list))*[tgt['tok2id']['<pad>']]
            mask = [0]*seq_len + [1]*(max_len-seq_len)
        else:
            seq_len = max_len
            wid_list = wid_list[:max_len]
            mask = [0]*seq_len
    return [wid_list], [seq_len], [mask]