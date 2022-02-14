#!/usr/bin/env python3

import argparse

import torch
import torch.nn.functional as F
import math
import numpy as np
from model.bert import BertClassifier
from bert_preprocessing import get_batch, Corpus, batchify, batchify_docs, get_batch_docs, file_to_df
import torch.optim as optim
from eval_seq2seq import eval
import os
import gc
import pandas as pd
from nltk.stem.porter import *
import pickle
from transformers import BertTokenizer, GPT2Tokenizer
from sklearn import model_selection
from run_configs import configs
from lemmagen3 import Lemmatizer
import LatvianStemmer

def count_parameters(model):
    return sum(p.numel() for p in model.parameters() if p.requires_grad)




def test_model(batch_size, learning_rate, n_ctx, n_head, n_embd, n_layer, adaptive, bpe, masked_lm, classification, bpe_model_path, datasets, lm_corpus_file, pos_tags, dict_path, rnn, crf, config_id,clf_model):
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--nsamples", type=int, default=1)
    parser.add_argument("--batch_size", type=int, default=batch_size)
    parser.add_argument("--length", type=int, default=-1)
    parser.add_argument("--temperature", type=int, default=1)
    parser.add_argument("--top_k", type=int, default=0)
    parser.add_argument('--unconditional', action='store_true', help='If true, unconditional generation.')

    parser.add_argument('--lr_schedule', type=str, default='warmup_linear')
    parser.add_argument('--lr_warmup', type=float, default=0.002)
    parser.add_argument('--lr', type=float, default=learning_rate)
    parser.add_argument('--b1', type=float, default=0.9)
    parser.add_argument('--b2', type=float, default=0.999)
    parser.add_argument('--e', type=float, default=1e-8)
    parser.add_argument('--l2', type=float, default=0.01)
    parser.add_argument('--vector_l2', action='store_true')
    parser.add_argument('--max_grad_norm', type=int, default=1)

    parser.add_argument("--initializer_range", type=float, default=0.02)
    parser.add_argument("--layer_norm_epsilon", type=float, default=1e-6)

    parser.add_argument("--n_ctx", type=int, default=n_ctx)
    parser.add_argument("--n_positions", type=int, default=n_ctx)
    parser.add_argument("--n_embd", type=int, default=n_embd)
    parser.add_argument("--n_head", type=int, default=n_head)
    parser.add_argument("--n_layer", type=int, default=n_layer)
    parser.add_argument("--max_vocab_size", type=int, default=0, help='Zero means no limit.')

    parser.add_argument('--max_step', type=int, default=100000, help='upper epoch limit')
    parser.add_argument('--eta_min', type=float, default=0.0, help='min learning rate for cosine scheduler')
    parser.add_argument('--clip', type=float, default=0.25, help='gradient clipping')
    parser.add_argument('--kw_cut', type=int, default=10, help='Precison and recall @')

    parser.add_argument("--num_epoch", type=int, default=10)

    parser.add_argument('--data_path', type=str, default='data')
    parser.add_argument('--result_path', type=str, default='bert_news_results.txt')

    parser.add_argument('--adaptive', action='store_true', help='If true, use adaptive softmax.')
    parser.add_argument('--bpe', action='store_true', help='If true, use byte pair encoding.')
    parser.add_argument('--masked_lm', action='store_true', help='If true, use masked language model objective for pretraining instead of regular language model.')
    parser.add_argument('--transfer_learning', action='store_true', help='If true, use a pretrained language model.')
    parser.add_argument('--POS_tags', action='store_true', help='POS tags')
    parser.add_argument('--classification', action='store_true', help='If true, train a classifier.')
    parser.add_argument('--rnn', action='store_true', help='If true, use a RNN with attention in classification head.')
    parser.add_argument('--crf', action='store_true', help='If true, use CRF instead of costum loss function in classification head.')

    parser.add_argument('--bpe_model_path', type=str, default=bpe_model_path)
    parser.add_argument('--clf_model', type=str, default=clf_model)

    parser.add_argument('--datasets', type=str, default=datasets)
    parser.add_argument('--lm_corpus_file', type=str, default=lm_corpus_file)
    parser.add_argument('--trained_language_models_dir', type=str, default='trained_language_models')
    parser.add_argument('--trained_classification_models_dir', type=str, default='trained_classification_models')

    parser.add_argument('--dict_path', type=str, default=dict_path, help='Path to dictionary')
    parser.add_argument('--lang', type=str, default='estonian', help='Path to dictionary')
    parser.add_argument('--config_id', type=str, default=config_id, help='Used to connect trained language models with classification models')
    parser.add_argument('--cuda', action='store_false', help='If true, use gpu.')

    clf_model = clf_model#[0]
    args = parser.parse_args()
    args.adaptive = adaptive
    args.classification = classification
    args.transfer_learning = True
    args.POS_tags = pos_tags
    args.bpe = bpe
    args.masked_lm = masked_lm
    args.rnn = rnn
    args.crf = crf
    args.cuda = True
    args.lang = args.datasets
    args.result_path = "bert_"+args.lang+"_"
    if not os.path.exists(args.trained_classification_models_dir):
        os.makedirs(args.trained_classification_models_dir)

    if not os.path.exists(args.trained_language_models_dir):
        os.makedirs(args.trained_language_models_dir)

    if args.bpe:
        sp = BertTokenizer.from_pretrained('bert-base-multilingual-uncased')
        #sp = GPT2Tokenizer.from_pretrained("gpt2")
    else:
        sp = None

    if args.crf:
        assert not args.rnn
    if args.rnn:
        assert not args.crf

    print(args)


    if args.lang=='english':
        stemmer = PorterStemmer()
    if args.lang == 'latvian':
        stemmer = LatvianStemmer
    if args.lang == 'croatian':
        stemmer = Lemmatizer('hr')
    if  args.lang == 'russian':
        stemmer = Lemmatizer('ru')
    if args.lang=='estonian':
        stemmer = Lemmatizer('et')


    np.random.seed(args.seed)
    torch.random.manual_seed(args.seed)
    torch.cuda.manual_seed(args.seed)
    result_file = open(args.result_path, 'a', encoding='utf8')
    result_file.write("Classification results for config " + config_id + ":\n\n")
    result_file.write("Parameters:\n")
    result_file.write(str(args) + '\n------------------------------------------------\n')
      
    for folder in args.datasets.split(';'):
      
        print('------------------------------------------------------------------------------------------------------')
        print('Training on: ', folder)
        print('------------------------------------------------------------------------------------------------------')
          
        
        df_train = file_to_df(os.path.join(args.data_path, folder, folder + '_valid.json'), classification=True)
        df_train = df_train.sample(frac=1, random_state=2019)
        val_idx = int(0.8 * df_train.shape[0])
        df_valid = df_train[:2]
        df_train = df_train[:2]
        df_test = file_to_df(os.path.join(args.data_path, folder, folder + '_test.json'), classification=True)
          
          
        print("Train size: ", df_train.shape, "Valid size: ", df_valid.shape, "Test size: ", df_test.shape)
        print()
          
        total_pred, total_true, num_parameters = train_test_test_only(df_train, df_valid, df_test, args, stemmer, sp, folder,clf_model)
          
        p_5, r_5, f_5, p_10, r_10, f_10, p_k, r_k, f_k, p_M, r_M, f_M = eval(total_pred, total_true, lang=args.lang)
          
        result_file.write("Dataset: " + folder + '\n')
        result_file.write('Precision@5: ' + str(p_5) + ' Recall@5: ' + str(r_5) + ' F1@5: ' + str(f_5) + '\n')
        result_file.write('Precision@10: ' + str(p_10) + ' Recall@10: ' + str(r_10) + ' F1@10: ' + str(f_10) + '\n')
        result_file.write('Precision@k: ' + str(p_k) + ' Recall@k: ' + str(r_k) + ' F1@k: ' + str(f_k) + '\n')
        result_file.write('Precision@M: ' + str(p_M) + ' Recall@M: ' + str(r_M) + ' F1@M: ' + str(f_M) + '\n')
        result_file.write('Num. trainable parameters: ' + str(num_parameters) + '\n')
          
        outputs = []
          
        for pred, true in zip(total_pred, total_true):
            pred = ";".join(list(pred))
            true = ";".join(list(true))
            outputs.append((pred, true))
              
        df_preds = pd.DataFrame(outputs, columns=['Predicted', 'True'])
        df_preds.to_csv('predictions/' + clf_model +"_"+ folder + '.csv', sep=',', encoding='utf8')

        result_file.write("\n-----------------------------------------------------------\n")
        result_file.write("\n-----------------------End of the run----------------------\n")
        result_file.write("\n-----------------------------------------------------------\n")
        result_file.close()
   


def train_test_test_only(df_train, df_valid, df_test, args, stemmer, sp, folder=None,clf_model=None):
    print('Producing dataset...')
    corpus = Corpus(df_train, df_valid, df_test, args)

    print()
    print('Batchifying')

    
    valid_keywords = corpus.valid_keywords
    test_keywords = corpus.test_keywords
    
    train_data, train_target = batchify_docs(corpus.train, corpus.train_target, args.batch_size)
    val_data, val_target = batchify_docs(corpus.valid, corpus.valid_target, args.batch_size)
    test_data, test_target = batchify_docs(corpus.test, corpus.test_target, 1)
    if args.POS_tags:
        train_pos, _ = batchify_docs(corpus.train_pos, corpus.train_target, args.batch_size)
        val_pos, _ = batchify_docs(corpus.valid_pos, corpus.valid_target, args.batch_size)
        test_pos, _ = batchify_docs(corpus.test_pos, corpus.test_target, 1)

    ntokens = len(corpus.dictionary)
    print('Vocabulary size: ', ntokens)
    args.vocab_size = ntokens

    # adaptive softmax / embedding
    cutoffs, tie_projs = [], [False]
    print("Adaptive softmax: ", args.adaptive)
    if args.adaptive:
        if not args.bpe:
            cutoffs = [20000, 40000, 200000]
        else:
            cutoffs = [20000, 30000]
        tie_projs += [True] * len(cutoffs)

    args.cutoffs = cutoffs
    args.tie_projs = tie_projs

    model = torch.load("trained_classification_models/"+clf_model)
    num_parameters = str(count_parameters(model))

    print()
    print('Testing on test set')
    print()

    if not args.POS_tags:
        test_pos = None

    total_loss, total_seq, total_pred, total_true = test(model, test_data, test_pos, test_target, corpus, args, stemmer, test_keywords, sp)
    total_loss = total_loss / total_seq

    gc.collect()
    del model


    if not args.classification:
        perplexity = math.exp(total_loss)
        print("Test loss: ", total_loss)
        print("Test set perplexity: ", perplexity)
        return None

    else:
        print()
        print('------------------------------------------------------------------------------------------------------------------')
        print()
        print('Testing on ', folder)
        #classification_models = os.listdir(args.trained_classification_models_dir)

        #for m in classification_models:
        #    clas_model_path = os.path.join(args.trained_classification_models_dir, m)
        #    os.remove(clas_model_path)

        return total_pred, total_true, num_parameters    


def run_model(batch_size, learning_rate, n_ctx, n_head, n_embd, n_layer, adaptive, bpe, masked_lm, classification, bpe_model_path, datasets, lm_corpus_file, pos_tags, dict_path, rnn, crf, config_id):
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--nsamples", type=int, default=1)
    parser.add_argument("--batch_size", type=int, default=batch_size)
    parser.add_argument("--length", type=int, default=-1)
    parser.add_argument("--temperature", type=int, default=1)
    parser.add_argument("--top_k", type=int, default=0)
    parser.add_argument('--unconditional', action='store_true', help='If true, unconditional generation.')

    parser.add_argument('--lr_schedule', type=str, default='warmup_linear')
    parser.add_argument('--lr_warmup', type=float, default=0.002)
    parser.add_argument('--lr', type=float, default=learning_rate)
    parser.add_argument('--b1', type=float, default=0.9)
    parser.add_argument('--b2', type=float, default=0.999)
    parser.add_argument('--e', type=float, default=1e-8)
    parser.add_argument('--l2', type=float, default=0.01)
    parser.add_argument('--vector_l2', action='store_true')
    parser.add_argument('--max_grad_norm', type=int, default=1)

    parser.add_argument("--initializer_range", type=float, default=0.02)
    parser.add_argument("--layer_norm_epsilon", type=float, default=1e-6)

    parser.add_argument("--n_ctx", type=int, default=n_ctx)
    parser.add_argument("--n_positions", type=int, default=n_ctx)
    parser.add_argument("--n_embd", type=int, default=n_embd)
    parser.add_argument("--n_head", type=int, default=n_head)
    parser.add_argument("--n_layer", type=int, default=n_layer)
    parser.add_argument("--max_vocab_size", type=int, default=0, help='Zero means no limit.')

    parser.add_argument('--max_step', type=int, default=100000, help='upper epoch limit')
    parser.add_argument('--eta_min', type=float, default=0.0, help='min learning rate for cosine scheduler')
    parser.add_argument('--clip', type=float, default=0.25, help='gradient clipping')
    parser.add_argument('--kw_cut', type=int, default=10, help='Precison and recall @')

    parser.add_argument("--num_epoch", type=int, default=10)

    parser.add_argument('--data_path', type=str, default='data')
    parser.add_argument('--result_path', type=str, default='bert_news_results.txt')

    parser.add_argument('--adaptive', action='store_true', help='If true, use adaptive softmax.')
    parser.add_argument('--bpe', action='store_true', help='If true, use byte pair encoding.')
    parser.add_argument('--masked_lm', action='store_true', help='If true, use masked language model objective for pretraining instead of regular language model.')
    parser.add_argument('--transfer_learning', action='store_true', help='If true, use a pretrained language model.')
    parser.add_argument('--POS_tags', action='store_true', help='POS tags')
    parser.add_argument('--classification', action='store_true', help='If true, train a classifier.')
    parser.add_argument('--rnn', action='store_true', help='If true, use a RNN with attention in classification head.')
    parser.add_argument('--crf', action='store_true', help='If true, use CRF instead of costum loss function in classification head.')

    parser.add_argument('--bpe_model_path', type=str, default=bpe_model_path)
    parser.add_argument('--datasets', type=str, default=datasets)
    parser.add_argument('--lm_corpus_file', type=str, default=lm_corpus_file)
    parser.add_argument('--trained_language_models_dir', type=str, default='trained_language_models')
    parser.add_argument('--trained_classification_models_dir', type=str, default='trained_classification_models')

    parser.add_argument('--dict_path', type=str, default=dict_path, help='Path to dictionary')
    parser.add_argument('--lang', type=str, default='estonian', help='Path to dictionary')
    parser.add_argument('--config_id', type=str, default=config_id, help='Used to connect trained language models with classification models')
    parser.add_argument('--cuda', action='store_false', help='If true, use gpu.')


    args = parser.parse_args()
    args.adaptive = adaptive
    args.classification = classification
    args.transfer_learning = True
    args.POS_tags = pos_tags
    args.bpe = bpe
    args.masked_lm = masked_lm
    args.rnn = rnn
    args.crf = crf
    args.cuda = True
    args.lang = args.datasets
    args.result_path = "bert_"+args.lang

    if not os.path.exists(args.trained_classification_models_dir):
        os.makedirs(args.trained_classification_models_dir)

    if not os.path.exists(args.trained_language_models_dir):
        os.makedirs(args.trained_language_models_dir)

    if args.bpe:
        sp = BertTokenizer.from_pretrained('bert-base-multilingual-uncased')
        #sp = GPT2Tokenizer.from_pretrained("gpt2")
    else:
        sp = None

    if args.crf:
        assert not args.rnn
    if args.rnn:
        assert not args.crf

    print(args)


    if args.lang=='english':
        stemmer = PorterStemmer()
    if args.lang == 'latvian':
        stemmer = LatvianStemmer
    if args.lang == 'croatian':
        stemmer = Lemmatizer('hr')
    if  args.lang == 'russian':
        stemmer = Lemmatizer('ru')
    if args.lang=='estonian':
        stemmer = Lemmatizer('et')


    np.random.seed(args.seed)
    torch.random.manual_seed(args.seed)
    torch.cuda.manual_seed(args.seed)

    if not args.classification:
        df_data = file_to_df(os.path.join(args.data_path, args.lm_corpus_file), classification=False)
        df_data = df_data.sample(frac=1, random_state=2019)
        val_idx = int(0.8 * df_data.shape[0])
        test_idx = int(0.9 * df_data.shape[0])
        df_train = df_data[:val_idx]
        df_valid = df_data[val_idx:test_idx]
        df_test = df_data[test_idx:]

        print('------------------------------------------------------------------------------------------------------')
        print('Training language model on all data')
        print("Train size: ", df_train.shape, "Valid size: ", df_valid.shape, "Test size: ", df_test.shape)
        print('------------------------------------------------------------------------------------------------------')
        print()
        train_test(df_train, df_valid, df_test, args, stemmer, sp)

    else:
        result_file = open(args.result_path, 'a', encoding='utf8')
        result_file.write("Classification results for config " + config_id + ":\n\n")
        result_file.write("Parameters:\n")
        result_file.write(str(args) + '\n------------------------------------------------\n')

        for folder in args.datasets.split(';'):

            print('------------------------------------------------------------------------------------------------------')
            print('Training on: ', folder)
            print('------------------------------------------------------------------------------------------------------')

        
            df_train = file_to_df(os.path.join(args.data_path, folder, folder + '_valid.json'), classification=True)
            df_train = df_train.sample(frac=1, random_state=2019)
            val_idx = int(0.8 * df_train.shape[0])
            df_valid = df_train[val_idx:]
            df_train = df_train[:val_idx]
            df_test = file_to_df(os.path.join(args.data_path, folder, folder + '_test.json'), classification=True)


            print("Train size: ", df_train.shape, "Valid size: ", df_valid.shape, "Test size: ", df_test.shape)
            print()

            total_pred, total_true, num_parameters = train_test(df_train, df_valid, df_test, args, stemmer, sp, folder)

            p_5, r_5, f_5, p_10, r_10, f_10, p_k, r_k, f_k, p_M, r_M, f_M = eval(total_pred, total_true, lang=args.lang)

            result_file.write("Dataset: " + folder + '\n')
            result_file.write('Precision@5: ' + str(p_5) + ' Recall@5: ' + str(r_5) + ' F1@5: ' + str(f_5) + '\n')
            result_file.write('Precision@10: ' + str(p_10) + ' Recall@10: ' + str(r_10) + ' F1@10: ' + str(f_10) + '\n')
            result_file.write('Precision@k: ' + str(p_k) + ' Recall@k: ' + str(r_k) + ' F1@k: ' + str(f_k) + '\n')
            result_file.write('Precision@M: ' + str(p_M) + ' Recall@M: ' + str(r_M) + ' F1@M: ' + str(f_M) + '\n')
            result_file.write('Num. trainable parameters: ' + str(num_parameters) + '\n')

            outputs = []

            for pred, true in zip(total_pred, total_true):
                pred = ";".join(list(pred))
                true = ";".join(list(true))
                outputs.append((pred, true))

            df_preds = pd.DataFrame(outputs, columns=['Predicted', 'True'])
            df_preds.to_csv('predictions/' + folder + '.csv', sep=',', encoding='utf8')

        result_file.write("\n-----------------------------------------------------------\n")
        result_file.write("\n-----------------------End of the run----------------------\n")
        result_file.write("\n-----------------------------------------------------------\n")
        result_file.close()




def train_test(df_train, df_valid, df_test, args, stemmer, sp, folder=None):
    print('Producing dataset...')
    corpus = Corpus(df_train, df_valid, df_test, args)

    print()
    print('Batchifying')

    if not args.classification:
        train_data = batchify(corpus.train, args.batch_size, args.n_ctx)
        val_data = batchify(corpus.valid, args.batch_size, args.n_ctx)
        test_data = batchify(corpus.test, args.batch_size, args.n_ctx)
        if args.POS_tags:
            train_pos = batchify(corpus.train_pos, args.batch_size, args.n_ctx)
            val_pos = batchify(corpus.valid_pos, args.batch_size, args.n_ctx)
            test_pos = batchify(corpus.test_pos, args.batch_size, args.n_ctx)

        val_target = None
        valid_keywords = None
        test_target = None
        test_keywords = None
    else:
        valid_keywords = corpus.valid_keywords
        test_keywords = corpus.test_keywords

        train_data, train_target = batchify_docs(corpus.train, corpus.train_target, args.batch_size)
        val_data, val_target = batchify_docs(corpus.valid, corpus.valid_target, args.batch_size)
        test_data, test_target = batchify_docs(corpus.test, corpus.test_target, 1)
        if args.POS_tags:
            train_pos, _ = batchify_docs(corpus.train_pos, corpus.train_target, args.batch_size)
            val_pos, _ = batchify_docs(corpus.valid_pos, corpus.valid_target, args.batch_size)
            test_pos, _ = batchify_docs(corpus.test_pos, corpus.test_target, 1)

    ntokens = len(corpus.dictionary)
    print('Vocabulary size: ', ntokens)
    args.vocab_size = ntokens

    # adaptive softmax / embedding
    cutoffs, tie_projs = [], [False]
    print("Adaptive softmax: ", args.adaptive)
    if args.adaptive:
        if not args.bpe:
            cutoffs = [20000, 40000, 200000]
        else:
            cutoffs = [20000, 30000]
        tie_projs += [True] * len(cutoffs)

    args.cutoffs = cutoffs
    args.tie_projs = tie_projs

    model = BertClassifier(args)
    model.cuda()

    optimizer = torch.optim.Adam(model.parameters(), lr=learning_rate)
    scheduler = optim.lr_scheduler.CosineAnnealingLR(optimizer, args.max_step, eta_min=args.eta_min)


    best_loss = 9999
    best_f = 0
    best_model_path = ''

    train_step = 0

    for epoch in range(args.num_epoch):
        print()
        print("Epoch: ", epoch + 1, "Num. train batches: ", train_data.size(1))
        print()

        model.train()

        total_loss = 0
        total_seq = 0

        i = 0
        cut = 0
        if not args.classification:
            cut = args.n_ctx
            all_steps = train_data.size(1)
        else:
            all_steps = train_data.size(0)


        while i < all_steps - cut:

            if not args.classification:
                encoder_words, batch_labels, mask = get_batch(train_data, i, args, corpus.dictionary.word2idx)
                if args.POS_tags:
                    encoder_pos, _, _= get_batch(train_pos, i, args, corpus.dictionary.word2idx, mask)
                #print("iNPUT SIZE: ", encoder_words.size(), decoder_words.size())
            else:
                encoder_words, batch_labels = get_batch_docs(train_data, train_target, i,)
                #batch_labels[batch_labels > 1] = 2
                if args.POS_tags:
                    encoder_pos, _ = get_batch_docs(train_pos, train_target, i, )
                #print("iNPUT SIZE: ", encoder_words.size(), decoder_words.size())
                mask = None

            if not args.POS_tags:
                encoder_pos=None

            optimizer.zero_grad()

            loss = model(encoder_words, input_pos=encoder_pos, lm_labels=batch_labels, masked_idx=mask)
            loss = loss.float().mean().type_as(loss)
            loss.backward()

            # `clip_grad_norm` helps prevent the exploding gradient problem in RNNs / LSTMs.
            torch.nn.utils.clip_grad_norm_(model.parameters(), args.clip)
            optimizer.step()

            train_step += 1
            scheduler.step()
            if args.classification:
                report_step = 32
            else:
                report_step = 10240
            if train_step % report_step == 0:
                print("Learning rate: ", optimizer.param_groups[0]['lr'])

            if not args.classification:
                i += args.n_ctx
                total_loss += batch_labels.size(0) * loss.item()
                total_seq += batch_labels.size(0)
            else:
                i += 1
                total_loss +=  loss.item()
                total_seq += 1

            if i % report_step == 0:
                print('Step: ', i, ' loss: ', total_loss/total_seq)


        #Validation
        print()
        print('Validating')
        print()
        if not args.POS_tags:
            val_pos = None


        total_loss, total_seq, total_pred, total_true = test(model, val_data, val_pos, val_target, corpus, args, stemmer, valid_keywords, sp)
        total_loss = total_loss/total_seq
        print("Total loss, total seq: ", total_loss, total_seq)
        print("Val shape: ", val_data.size())

        if args.classification:
            print('Validating on ', folder)
            p_5, r_5, f_5, p_10, r_10, f_10, p_k, r_k, f_k, p_M, r_M, f_M = eval(total_pred, total_true, lang=args.lang)
            score = str(total_loss)

        else:
            perplexity = math.exp(total_loss)
            score = str(perplexity)[:6]
            print("Validation loss: ", total_loss)
            print("Validation set perplexity: ", perplexity)

        if not args.classification:
            if  total_loss < best_loss:
                path = os.path.join(args.trained_language_models_dir, "model_" + args.config_id + "_perp_" + score + "_epoch_" + str(epoch + 1) + ".pt")
                with open(path, 'wb') as f:
                    print('Saving model')
                    torch.save(model, f)

                #delete all models but the best
                if best_model_path:
                    if os.path.isfile(best_model_path):
                        os.remove(best_model_path)

                best_model_path = path
                best_loss = total_loss
        else:
            if f_10 > best_f:
                path = os.path.join(args.trained_classification_models_dir, "model_" + args.config_id + "_folder_" + folder +  "_loss_" + score + "_epoch_" + str(epoch + 1) + ".pt")
                with open(path, 'wb') as f:
                    print('Saving model')
                    torch.save(model, f)

                # delete all models but the best
                if best_model_path:
                    if os.path.isfile(best_model_path):
                        os.remove(best_model_path)
                best_model_path = path
                best_f = f_10

        gc.collect()

    del model
    del optimizer
    del scheduler

    model = torch.load(best_model_path)
    num_parameters = str(count_parameters(model))

    print()
    print('Testing on test set')
    print()

    if not args.POS_tags:
        test_pos = None

    total_loss, total_seq, total_pred, total_true = test(model, test_data, test_pos, test_target, corpus, args, stemmer, test_keywords, sp)
    total_loss = total_loss / total_seq

    gc.collect()
    del model


    if not args.classification:
        perplexity = math.exp(total_loss)
        print("Test loss: ", total_loss)
        print("Test set perplexity: ", perplexity)
        return None

    else:
        print()
        print('------------------------------------------------------------------------------------------------------------------')
        print()
        print('Testing on ', folder)
        #classification_models = os.listdir(args.trained_classification_models_dir)

        #for m in classification_models:
        #    clas_model_path = os.path.join(args.trained_classification_models_dir, m)
        #    os.remove(clas_model_path)

        return total_pred, total_true, num_parameters



def test(model, data, data_pos, target, corpus, args, stemmer, keywords=None, sp=None):
    # testing

    total_pred = []
    total_true = []
    total_loss = 0
    total_seq = 0

    model.eval()
    if not args.classification:
        step = args.n_ctx
        cut = args.n_ctx
        all_steps = data.size(1)
    else:
        step = 1
        cut = 0
        all_steps = data.size(0)

    if not args.POS_tags:
        encoder_pos=None

    with torch.no_grad():

        all_predicted_save = []
        all_true_save = []

        for i in range(0, all_steps - cut, step):

            if not args.classification:
                encoder_words, batch_labels, mask = get_batch(data, i, args, corpus.dictionary.word2idx)
                if args.POS_tags:
                    encoder_pos, _, _ = get_batch(data_pos, i, args, corpus.dictionary.word2idx, mask)
            else:
                encoder_words, batch_labels = get_batch_docs(data, target, i)
                if args.POS_tags:
                    encoder_pos, _ = get_batch_docs(data_pos, target, i)
                mask = None


            input_batch_labels = batch_labels.clone()

            if not args.classification:
                loss, logits = model(encoder_words, input_pos=encoder_pos, lm_labels=input_batch_labels, masked_idx=mask, test=True)
            else:
                if not args.crf:
                    loss, logits = model(encoder_words, input_pos=encoder_pos, lm_labels=input_batch_labels, test=True)
                else:
                    loss, logits, crf_preds = model(encoder_words, input_pos=encoder_pos, lm_labels=input_batch_labels, test=True)

            loss = loss.mean()
            total_loss += batch_labels.size(0) * loss.float().item()
            total_seq += batch_labels.size(0)

            if args.classification:
                report_step = 32
            else:
                report_step = 10240

            if i % report_step == 0:
                print('Eval step: ', i, 'Loss: ', total_loss/total_seq)

            if args.classification:

                maxes = []
                true_y = []

                for batch in encoder_words.cpu().numpy():
                    key = "".join([str(x) for x in batch])
                    true_example = keywords[key]
                    true_example = [" ".join(kw) for kw in true_example]
                    true_y.append(true_example)
                    all_true_save.append(true_example)

                batch_counter = 0
                for batch_idx, batch in enumerate(logits):
                    #print("Preds: ", crf_preds[batch_idx])
                    #print("True: ", input_batch_labels[batch_idx].cpu().numpy().tolist())
                    #print('---------------------------------------------------------------')

                    pred_save = []

                    pred_example = []
                    batch = F.softmax(batch, dim=1)
                    #print("predictions batch: ", batch.max(1)[1])
                    length = batch.size(0)
                    position = 0

                    pred_vector = []
                    probs_dict = {}

                    while position < len(batch):
                        pred = batch[position]
                        if not args.crf:
                            _ , idx = pred.max(0)
                            idx = idx.item()
                        else:
                            idx = crf_preds[batch_idx][position]

                        pred_vector.append(pred)
                        pred_word = []

                        if idx == 1:
                            words = []
                            num_steps = length - position
                            for j in range(num_steps):
                                new_pred = batch[position + j]
                                values, new_idx = new_pred.max(0)

                                if not args.crf:
                                    new_idx = new_idx.item()
                                else:
                                    new_idx = crf_preds[batch_idx][position + j]
                                prob = values.item()

                                if new_idx == 1:
                                    word = corpus.dictionary.idx2word[encoder_words[batch_counter][position + j].item()]
                                    #print(corpus.dictionary.idx2word[100],corpus.dictionary.idx2word[0], corpus.dictionary.idx2word[101], corpus.dictionary.idx2word[99])
                                    words.append((word, prob))
                                    pred_word.append((word, prob))

                                    #add max word prob in document to prob dictionary
                                    if args.lang=='english' or args.lang=='latvian':
                                        stem = stemmer.stem(word)
                                    elif args.lang=='estonian' or args.lang == 'russian' or args.lang=='croatian':
                                        stem = stemmer.lemmatize(word)

                                    if stem not in probs_dict:
                                        probs_dict[stem] = prob
                                    else:
                                        if probs_dict[stem] < prob:
                                            probs_dict[stem] = prob
                                else:
                                    if sp is not None:
                                        word = corpus.dictionary.idx2word[encoder_words[batch_counter][position + j].item()]
                                        #if not word.startswith('Ġ'):
                                        if word.startswith('##'):
                                            words.append((word, prob))

                                            # add max word prob in document to prob dictionary
                                            if args.lang=='english' or args.lang=='latvian':
                                                stem = stemmer.stem(word)
                                            elif args.lang=='estonian' or args.lang == 'russian' or args.lang=='croatian':
                                                stem = stemmer.lemmatize(word)

                                            if stem not in probs_dict:
                                                probs_dict[stem] = prob
                                            else:
                                                if probs_dict[stem] < prob:
                                                    probs_dict[stem] = prob
                                    break

                            position += j + 1
                            words = [x[0] for x in words]
                            pred_example.append(words)
                            pred_save.append(pred_word)
                        else:
                            position += 1

                    all_predicted_save.append(pred_save)


                    #assign probabilities
                    pred_examples_with_probs = []
                    #print("Candidate: ", pred_example)
                    for kw in pred_example:
                        probs = []
                        for word in kw:
                            if args.lang=='english' or args.lang=='latvian':
                                stem = stemmer.stem(word)
                            elif args.lang=='estonian' or args.lang == 'russian' or args.lang=='croatian':
                                stem = stemmer.lemmatize(word)

                            probs.append(probs_dict[stem])

                        kw_prob = sum(probs)/len(probs)
                        pred_examples_with_probs.append((" ".join(kw), kw_prob))

                    pred_example = pred_examples_with_probs

                    #sort by softmax probability
                    pred_example = sorted(pred_example, reverse=True, key=lambda x: x[1])

                    #remove keywords that contain punctuation and duplicates
                    all_kw = set()
                    filtered_pred_example = []
                    kw_stems = []

                    punctuation = "!$%&'()*+,.:;<=>?@[\]^_`{|}~"

                    for kw, prob in pred_example:
                        kw_decoded = kw.replace(' ##', '')
                        #kw_decoded = kw.replace(' ', '')
                        #kw_decoded = kw_decoded.replace('Ġ', ' ').strip()
                        if args.lang=='english' or args.lang=='latvian':
                            kw_stem = " ".join([stemmer.stem(word) for word in kw_decoded.split()])
                        elif args.lang=='estonian' or args.lang == 'russian' or args.lang=='croatian':
                            kw_stem = " ".join([stemmer.lemmatize(word) for word in kw_decoded.split()])
                        kw_stems.append(kw_stem)

                        if kw_stem not in all_kw and len(kw_stem.split()) == len(set(kw_stem.split())):
                            has_punct = False
                            for punct in punctuation:
                                if punct in kw:
                                    has_punct = True
                                    break
                            kw_decoded = kw.replace(' ##', '')
                            #kw_decoded = kw.replace(' ', '')
                            #kw_decoded = kw_decoded.replace('Ġ', ' ').strip()
                            if not has_punct and len(kw_decoded.split()) < 5:
                                filtered_pred_example.append((kw, prob))
                        all_kw.add(kw_stem)

                    pred_example = filtered_pred_example
                    filtered_pred_example = [x[0] for x in pred_example][:args.kw_cut]

                    maxes.append(filtered_pred_example)
                    batch_counter += 1

                if sp is not None:
                    all_decoded_maxes = []
                    all_decoded_true_y = []
                    for doc in maxes:
                        decoded_maxes = []
                        for kw in doc:
                            kw = kw.replace(' ##', '')
                            #kw = kw.replace(' ', '')
                            #kw = kw.replace('Ġ', ' ').strip()
                            #print("Predicted: ", kw)
                            decoded_maxes.append(kw)
                        all_decoded_maxes.append(decoded_maxes)
                    for doc in true_y:
                        decoded_true_y = []
                        for kw in doc:
                            kw = kw.replace(' ##', '')
                            #kw = kw.replace(' ', '')
                            #kw = kw.replace('Ġ', ' ').strip()
                            #print("True: ", kw)
                            decoded_true_y.append(kw)
                        all_decoded_true_y.append(decoded_true_y)

                    maxes = all_decoded_maxes
                    true_y = all_decoded_true_y
                    #print("Predicted: ", maxes)
                    #print("True: ", true_y)

                total_pred.extend(maxes)
                total_true.extend(true_y)

            # print(len(total_pred), total_pred[:10])
            # print(len(total_true), total_true[:10])

        with open('preds.pickle', 'wb') as file:
            pickle.dump((all_predicted_save, all_true_save), file)

    return total_loss, total_seq, total_pred, total_true



if __name__ == '__main__':
    import os
    #tmp = 'estonian_russian'
    things = os.listdir('trained_classification_models')
    things = sorted(things, key=len)#,reverse=True)
    mapper = {}
    langs = ['croatian','estonian','latvian','russian']
    for f in os.listdir('trained_classification_models'):
        clfs = f.split('+')[1].split('loss')[0][:-1].split('_folder_')[0] 
        if not "rnn" in f: 
            for x in clfs.split('_'):
                if x not in langs:
                    continue
            mapper[clfs] = f 
    
    configs = list()
    for m in mapper:
        if m == 'croatian_russian': continue
        base_model = m#'latvian_russian'
        clf_model = mapper[m]#'model_bpe+latvian_russian_folder_latvian_russian_loss_0.0634551762614623_epoch_8.pt'
        for lang in langs:
            cfg_ =  {'id': 'bpe+'+base_model,
             'classification': True,
             'adaptive': False,
             'transfer_learning': False,
             'POS_tags': False,
             'bpe': True,
             'bpe_model_path': 'data/bpe_'+base_model+'_big.model',
             'masked_lm': True,
             'rnn': True,
             'crf': False,
             'clf_model': clf_model,
             'datasets': lang,
             'lm_corpus_file': base_model+'.json'
             }     
            configs.append(cfg_)
    for idx, config in enumerate(configs):
        print()
        print('running config: ', config['id'])
        print()
        clf_model = config['clf_model']
        adaptive = config['adaptive']
        classification = config['classification']
        transfer_learning = config['transfer_learning']
        pos_tags = config['POS_tags']
        bpe = config['bpe']
        bpe_model_path = config['bpe_model_path']
        masked_lm = config['masked_lm']
        datasets = config['datasets']
        lm_corpus_file = config['lm_corpus_file']
        rnn = config['rnn']
        crf = config['crf']

        if crf:
            dcrf = 'crf'
        else:
            dcrf = 'nocrf'

        if rnn:
            drnn = 'rnn'
        else:
            drnn = 'nornn'
        if adaptive:
            dadaptive = 'adaptive'
        else:
            dadaptive = 'noadaptive'
        if masked_lm:
            dmasked_lm = 'maskedlm'
        else:
            dmasked_lm = 'lm'
        if bpe:
            dbpe = 'bpe'
        else:
            dbpe = 'nobpe'
        if pos_tags:
            dpos = 'pos'
        else:
            dpos = 'nopos'


        if not classification:
            dict_path = 'dictionaries/dict_' + config['id'] + '_' + dadaptive + '_' + dmasked_lm + '_' + dbpe + '_' + dpos + '_' + drnn + '_' + dcrf + '.ptb'
        else:
            dict_path = 'dictionaries/dict_' + config['id'] + '_' + dbpe + '_' + dpos + '_' + drnn + '_' + dcrf + '.ptb'

        bs = [8]
        lr = [3e-5]
        #lr = [0.0003]
        n_ctxs = [256]
        n_heads = [8]
        n_embds = [768]
        n_layers = [8]

        counter=0

        r = 'lm_results.txt'
        with open(r, 'a', encoding='utf8') as f:

            for batch_size in bs:
                for learning_rate in lr:
                    for n_ctx in n_ctxs:
                        for n_head in n_heads:
                            for n_embd in n_embds:
                                for n_layer in n_layers:
                                    counter += 1
                                    test_model(batch_size, learning_rate, n_ctx, n_head, n_embd, n_layer, adaptive, bpe, masked_lm, classification, bpe_model_path, datasets, lm_corpus_file, pos_tags, dict_path, rnn, crf, config['id'],clf_model)
                                    f.write('Parameters:\n')
                                    f.write('batch size: ' + str(batch_size) + '\n')
                                    f.write('lr: ' + str(learning_rate) + '\n')
                                    f.write('n_ctx: ' + str(n_ctx) + '\n')
                                    f.write('n_head: ' + str(n_head) + '\n')
                                    f.write('n_embd: ' + str(n_embd) + '\n')
                                    f.write('n_layer: ' + str(n_layer) + '\n\n')
                                    f.write('Results:\n')
                                    f.write('\n\n---------------------------------------------\n\n')



'''
Dataset: estonian no lemmatization
Precision@5: 0.38950665017012065 Recall@5: 0.33025661306455517 F1@5: 0.3574
Precision@10: 0.38710513847770267 Recall@10: 0.34021196572943846 F1@10: 0.3621
Precision@k: 0.4057235895698624 Recall@k: 0.302449420253469 F1@k: 0.3466
Precision@M: 0.38710513847770267 Recall@M: 0.34021196572943846 F1@M: 0.3621
Num. trainable parameters: 176808972
'''
