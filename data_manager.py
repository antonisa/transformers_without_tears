from os.path import join
import numpy as np
import torch
import all_constants as ac
import utils as ut

np.random.seed(ac.SEED)


class DataManager(object):
    def __init__(self, args):
        super(DataManager, self).__init__()
        self.args = args
        self.pairs = args.pairs.split(',')
        self.lang_vocab, self.lang_ivocab = ut.init_vocab(join(args.data_dir, 'lang.vocab'))
        self.vocab, self.ivocab = ut.init_vocab(join(args.data_dir, 'vocab.joint'))
        self.logit_masks = {}
        for lang in self.lang_vocab:
            mask = np.load(join(args.data_dir, 'mask.{}.npy'.format(lang)))
            self.logit_masks[lang] = torch.from_numpy(mask)

    def load_data(self):
        self.data = {}
        data_dir = self.args.data_dir
        batch_size = self.args.batch_size
        for pair in self.pairs:
            self.data[pair] = {}
            src_lang, tgt_lang = pair.split('2')
            for mode in [ac.TRAIN, ac.DEV]:
                src_file = join(data_dir, '{}/{}.{}.npy'.format(pair, mode, src_lang))
                guess_file = join(data_dir, '{}/{}.guess.{}.npy'.format(pair, mode, tgt_lang))
                tgt_file = join(data_dir, '{}/{}.target.{}.npy'.format(pair, mode, tgt_lang))
                src = np.load(src_file)
                guess = np.load(guess_file)
                tgt = np.load(tgt_file)
                self.args.logger.info('Loading {}-{}'.format(pair, mode))
                self.data[pair][mode] = PEDataset(src, guess, tgt, batch_size)

        # batch sampling similar to in preprocessing.py
        ns = np.array([len(self.data[pair][ac.TRAIN]) for pair in self.pairs])
        ps = ns / sum(ns)
        ps = ps ** self.args.alpha
        ps = ps / sum(ps)
        self.ps = ps
        self.args.logger.info('Sampling batches with probs:')
        for idx, pair in enumerate(self.pairs):
            self.args.logger.info('{}, n={}, p={}'.format(pair, ns[idx], ps[idx]))

        self.train_iters = {}
        for pair in self.pairs:
            self.train_iters[pair] = self.data[pair][ac.TRAIN].get_iter(shuffle=True)

        # load dev translate batches
        self.translate_data = {}
        for pair in self.pairs:
            src_lang, tgt_lang = pair.split('2')
            src_file = join(data_dir, '{}/{}.{}.bpe'.format(pair, ac.DEV, src_lang))
            guess_file = join(data_dir, '{}/{}.guess.{}.bpe'.format(pair, ac.DEV, tgt_lang))
            ref_file = join(data_dir, '{}/{}.target.{}'.format(pair, ac.DEV, tgt_lang))
            self.args.logger.info('Loading dev translate batches')
            src_batches, guess_batches, sorted_idxs = self.get_translate_batches(src_file, guess_file, batch_size=batch_size)
            self.translate_data[pair] = {
                'src_batches': src_batches,
                'guess_batches': guess_batches,
                'sorted_idxs': sorted_idxs,
                'ref_file': ref_file
            }

    def get_batch(self):
        pair = np.random.choice(self.pairs, p=self.ps)
        try:
            src, guess, tgt, targets = next(self.train_iters[pair])
        except StopIteration:
            self.train_iters[pair] = self.data[pair][ac.TRAIN].get_iter(shuffle=True)
            src, guess, tgt, targets = next(self.train_iters[pair])

        src_lang, tgt_lang = pair.split('2')
        return {
            'src': src,
            'guess': guess,
            'tgt': tgt,
            'targets': targets,
            'src_lang_idx': self.lang_vocab[src_lang],
            'guess_lang_idx': self.lang_vocab[tgt_lang],
            'tgt_lang_idx': self.lang_vocab[tgt_lang],
            'pair': pair,
            'logit_mask': self.logit_masks[tgt_lang]
        }

    def get_translate_batches(self, src_file, guess_file, batch_size=4096):
        sdata = []
        slens = []
        gdata = []
        glens = []
        with open(src_file, 'r') as fin:
            for line in fin:
                toks = line.strip().split()
                if toks:
                    ids = [self.vocab.get(tok, ac.UNK_ID) for tok in toks] + [ac.EOS_ID]
                    sdata.append(ids)
                    slens.append(len(ids))

        with open(guess_file, 'r') as fin:
            for line in fin:
                gtoks = line.strip().split()
                if gtoks:
                    ids = [self.vocab.get(tok, ac.UNK_ID) for tok in gtoks] + [ac.EOS_ID]
                    gdata.append(ids)
                    glens.append(len(ids))


        slens = np.array(slens)
        sdata = np.array(sdata)
        glens = np.array(glens)
        gdata = np.array(gdata)
        sorted_idxs = np.argsort(slens)
        slens = slens[sorted_idxs]
        sdata = sdata[sorted_idxs]
        glens = glens[sorted_idxs]
        gdata = gdata[sorted_idxs]

        # create batches
        src_batches = []
        guess_batches = []
        s_idx = 0
        length = sdata.shape[0]
        while s_idx < length:
            e_idx = s_idx + 1
            max_in_batch = slens[s_idx]
            gmax_in_batch = glens[s_idx]
            while e_idx < length:
                max_in_batch = max(max_in_batch, slens[e_idx])
                gmax_in_batch = max(gmax_in_batch, glens[e_idx])
                count = (e_idx - s_idx + 1) * 2 * max(max_in_batch, gmax_in_batch)
                if count > batch_size:
                    break
                else:
                    e_idx += 1

            max_in_batch = max(slens[s_idx:e_idx])
            gmax_in_batch = max(glens[s_idx:e_idx])
            src = np.zeros((e_idx - s_idx, max_in_batch), dtype=np.int32)
            guess = np.zeros((e_idx - s_idx, gmax_in_batch), dtype=np.int32)
            for i in range(s_idx, e_idx):
                src[i - s_idx] = list(sdata[i]) + (max_in_batch - slens[i]) * [ac.PAD_ID]
                guess[i - s_idx] = list(gdata[i]) + (gmax_in_batch - glens[i]) * [ac.PAD_ID]
            src_batches.append(torch.from_numpy(src).type(torch.long))
            guess_batches.append(torch.from_numpy(guess).type(torch.long))
            s_idx = e_idx

        return src_batches, guess_batches, sorted_idxs


class NMTDataset(object):
    def __init__(self, src, tgt, batch_size):
        super(NMTDataset, self).__init__()
        if src.shape[0] != tgt.shape[0]:
            raise ValueError('src and tgt must have the same size')

        self.batch_size = batch_size
        self.batches = []

        sorted_idxs = np.argsort([len(x) for x in src])
        src = src[sorted_idxs]
        tgt = tgt[sorted_idxs]
        src_lens = [len(x) for x in src]
        tgt_lens = [len(x) for x in tgt]

        # prepare batches
        s_idx = 0
        while s_idx < src.shape[0]:
            e_idx = s_idx + 1
            max_src_in_batch = src_lens[s_idx]
            max_tgt_in_batch = tgt_lens[s_idx]
            while e_idx < src.shape[0]:
                max_src_in_batch = max(max_src_in_batch, src_lens[e_idx])
                max_tgt_in_batch = max(max_tgt_in_batch, tgt_lens[e_idx])
                num_toks = (e_idx - s_idx + 1) * max(max_src_in_batch, max_tgt_in_batch)
                if num_toks > self.batch_size:
                    break
                else:
                    e_idx += 1

            batch = self.prepare_one_batch(
                src[s_idx:e_idx],
                tgt[s_idx:e_idx],
                src_lens[s_idx:e_idx],
                tgt_lens[s_idx:e_idx])
            self.batches.append(batch)
            s_idx = e_idx

        self.indices = list(range(len(self.batches)))

    def __len__(self):
        return len(self.batches)

    def prepare_one_batch(self, src, tgt, src_lens, tgt_lens):
        num_sents = len(src)
        max_src_len = max(src_lens)
        max_tgt_len = max(tgt_lens)

        src_batch = np.zeros([num_sents, max_src_len], dtype=np.int32)
        tgt_batch = np.zeros([num_sents, max_tgt_len], dtype=np.int32)
        target_batch = np.zeros([num_sents, max_tgt_len], dtype=np.int32)

        for i in range(num_sents):
            src_batch[i] = src[i] + (max_src_len - src_lens[i]) * [ac.PAD_ID]
            tgt_batch[i] = tgt[i] + (max_tgt_len - tgt_lens[i]) * [ac.PAD_ID]
            target_batch[i] = tgt[i][1:] + [ac.EOS_ID] + (max_tgt_len - tgt_lens[i]) * [ac.PAD_ID]

        src_batch = torch.from_numpy(src_batch).type(torch.long)
        tgt_batch = torch.from_numpy(tgt_batch).type(torch.long)
        target_batch = torch.from_numpy(target_batch).type(torch.long)
        return src_batch, tgt_batch, target_batch

    def get_iter(self, shuffle=False):
        if shuffle:
            np.random.shuffle(self.indices)

        for idx in self.indices:
            yield self.batches[idx]


class PEDataset(object):
    def __init__(self, src, guess, tgt, batch_size):
        super(PEDataset, self).__init__()
        if src.shape[0] != tgt.shape[0]:
            raise ValueError('src and tgt must have the same size')
        if src.shape[0] != guess.shape[0]:
            raise ValueError('src and guess must have the same size')
        if tgt.shape[0] != guess.shape[0]:
            raise ValueError('tgt and guess must have the same size')

        self.batch_size = batch_size
        self.batches = []

        sorted_idxs = np.argsort([len(x) for x in src])
        src = src[sorted_idxs]
        tgt = tgt[sorted_idxs]
        guess = guess[sorted_idxs]
        src_lens = [len(x) for x in src]
        tgt_lens = [len(x) for x in tgt]
        guess_lens = [len(x) for x in guess]

        # prepare batches
        s_idx = 0
        while s_idx < src.shape[0]:
            e_idx = s_idx + 1
            max_src_in_batch = src_lens[s_idx]
            max_guess_in_batch = guess_lens[s_idx]
            max_tgt_in_batch = tgt_lens[s_idx]
            while e_idx < src.shape[0]:
                max_src_in_batch = max(max_src_in_batch, src_lens[e_idx])
                max_guess_in_batch = max(max_guess_in_batch, guess_lens[e_idx])
                max_tgt_in_batch = max(max_tgt_in_batch, tgt_lens[e_idx])
                num_toks = (e_idx - s_idx + 1) * max(max_src_in_batch, max_tgt_in_batch, max_guess_in_batch)
                if num_toks > self.batch_size:
                    break
                else:
                    e_idx += 1

            batch = self.prepare_one_batch(
                src[s_idx:e_idx],
                guess[s_idx:e_idx],
                tgt[s_idx:e_idx],
                src_lens[s_idx:e_idx],
                guess_lens[s_idx:e_idx],
                tgt_lens[s_idx:e_idx])
            self.batches.append(batch)
            s_idx = e_idx

        self.indices = list(range(len(self.batches)))

    def __len__(self):
        return len(self.batches)

    def prepare_one_batch(self, src, guess, tgt, src_lens, guess_lens, tgt_lens):
        num_sents = len(src)
        max_src_len = max(src_lens)
        max_guess_len = max(guess_lens)
        max_tgt_len = max(tgt_lens)

        src_batch = np.zeros([num_sents, max_src_len], dtype=np.int32)
        guess_batch = np.zeros([num_sents, max_guess_len], dtype=np.int32)
        tgt_batch = np.zeros([num_sents, max_tgt_len], dtype=np.int32)
        target_batch = np.zeros([num_sents, max_tgt_len], dtype=np.int32)

        for i in range(num_sents):
            src_batch[i] = src[i] + (max_src_len - src_lens[i]) * [ac.PAD_ID]
            guess_batch[i] = guess[i] + (max_guess_len - guess_lens[i]) * [ac.PAD_ID]
            tgt_batch[i] = tgt[i] + (max_tgt_len - tgt_lens[i]) * [ac.PAD_ID]
            target_batch[i] = tgt[i][1:] + [ac.EOS_ID] + (max_tgt_len - tgt_lens[i]) * [ac.PAD_ID]

        src_batch = torch.from_numpy(src_batch).type(torch.long)
        guess_batch = torch.from_numpy(guess_batch).type(torch.long)
        tgt_batch = torch.from_numpy(tgt_batch).type(torch.long)
        target_batch = torch.from_numpy(target_batch).type(torch.long)
        return src_batch, guess_batch, tgt_batch, target_batch

    def get_iter(self, shuffle=False):
        if shuffle:
            np.random.shuffle(self.indices)

        for idx in self.indices:
            yield self.batches[idx]