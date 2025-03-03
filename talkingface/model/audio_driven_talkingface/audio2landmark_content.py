import torch
import torch.nn as nn
from torch.nn import functional as F
from torch.nn.init import xavier_normal_, constant_
from tqdm import tqdm
from os import listdir, path
import numpy as np
import os, subprocess
from glob import glob
import cv2

from talkingface.model.layers import Conv2d, Conv2dTranspose, nonorm_Conv2d
from talkingface.model.abstract_talkingface import AbstractTalkingFace
from talkingface.data.dataprocess.wav2lip_process import Wav2LipPreprocessForInference, Wav2LipAudio
from talkingface.utils import ensure_dir

import torch
import torch.nn as nn
import torch.nn.parallel
import torch.utils.data
import math
import torch.nn.functional as F
import copy
import numpy as np

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

AUDIO_FEAT_SIZE = 161
FACE_ID_FEAT_SIZE = 204
Z_SIZE = 16
EPSILON = 1e-40


class Audio2landmark_content(nn.Module):

    def __init__(self, num_window_frames=18, in_size=80, lstm_size=AUDIO_FEAT_SIZE, use_prior_net=False, hidden_size=256, num_layers=3, drop_out=0, bidirectional=False):
        super(Audio2landmark_content, self).__init__()

        self.fc_prior = self.fc = nn.Sequential(
            nn.Linear(in_features=in_size, out_features=256),
            nn.BatchNorm1d(256),
            nn.LeakyReLU(0.2),
            nn.Linear(256, lstm_size),
        )

        self.use_prior_net = use_prior_net
        if(use_prior_net):
            self.bilstm = nn.LSTM(input_size=lstm_size,
                                  hidden_size=hidden_size,
                                  num_layers=num_layers,
                                  dropout=drop_out,
                                  bidirectional=bidirectional,
                                  batch_first=True, )
        else:
            self.bilstm = nn.LSTM(input_size=in_size,
                                  hidden_size=hidden_size,
                                  num_layers=num_layers,
                                  dropout=drop_out,
                                  bidirectional=bidirectional,
                                  batch_first=True, )

        self.in_size = in_size
        self.lstm_size = lstm_size
        self.num_window_frames = num_window_frames

        self.fc_in_features = hidden_size * 2 if bidirectional else hidden_size
        self.fc = nn.Sequential(
            nn.Linear(in_features=self.fc_in_features + FACE_ID_FEAT_SIZE, out_features=512),
            nn.BatchNorm1d(512),
            nn.LeakyReLU(0.2),
            nn.Linear(512, 256),
            nn.BatchNorm1d(256),
            nn.LeakyReLU(0.2),
            nn.Linear(256, 204),
        )



    def forward(self, au, face_id):

        inputs = au
        if(self.use_prior_net):
            inputs = self.fc_prior(inputs.contiguous().view(-1, self.in_size))
            inputs = inputs.view(-1, self.num_window_frames, self.lstm_size)

        output, (hn, cn) = self.bilstm(inputs)
        output = output[:, -1, :]

        if(face_id.shape[0] == 1):
            face_id = face_id.repeat(output.shape[0], 1)
        output2 = torch.cat((output, face_id), dim=1)

        output2 = self.fc(output2)
        # output += face_id

        return output2, face_id
    
    def generate_batch(self):
        # main_end2end
        
#         print(file_dict)
        
        import sys
        sys.path.append('talkingface/utils/thirdparty/AdaptiveWingLoss')
        import os, glob
        import numpy as np
        import cv2
        import argparse
        from talkingface.utils.approaches.train_image_translation import Image_translation_block
        import torch
        import pickle
        import face_alignment
        from talkingface.utils.src.autovc.AutoVC_mel_Convertor_retrain_version import AutoVC_mel_Convertor
        import shutil
        import talkingface.utils.util.utils as util
        import yaml
        from scipy.signal import savgol_filter

        from talkingface.utils.approaches.train_audio2landmark import Audio2landmark_model

        default_head_name = 'dali'
        ADD_NAIVE_EYE = True
        CLOSE_INPUT_FACE_MOUTH = False
        
        yaml_file_path = 'talkingface/properties/model/Audio2landmark_content.yaml'

        # 打开并读取 YAML 文件
        with open(yaml_file_path, 'r') as file:
            # 使用 load 函数加载 YAML 文件的内容
            config = yaml.load(file, Loader=yaml.FullLoader)
            
        print(config)
        opt_parser=config
        


        ''' STEP 1: preprocess input single image '''
        img =cv2.imread('dataset/examples/' + config['jpg'])
        predictor = face_alignment.FaceAlignment(face_alignment.LandmarksType.THREE_D, device='cuda', flip_input=True)
        shapes = predictor.get_landmarks(img)
        if (not shapes or len(shapes) != 1):
            print('Cannot detect face landmarks. Exit.')
            exit(-1)
        shape_3d = shapes[0]

        if(config['close_input_face_mouth']):
            util.close_input_face_mouth(shape_3d)


        ''' Additional manual adjustment to input face landmarks (slimmer lips and wider eyes) '''
        # shape_3d[48:, 0] = (shape_3d[48:, 0] - np.mean(shape_3d[48:, 0])) * 0.95 + np.mean(shape_3d[48:, 0])
        shape_3d[49:54, 1] += 1.
        shape_3d[55:60, 1] -= 1.
        shape_3d[[37,38,43,44], 1] -=2
        shape_3d[[40,41,46,47], 1] +=2


        ''' STEP 2: normalize face as input to audio branch '''
        shape_3d, scale, shift = util.norm_input_face(shape_3d)


        ''' STEP 3: Generate audio data as input to audio branch '''
        # audio real data
        au_data = []
        au_emb = []
        ains = glob.glob1('dataset/examples', '*.wav')
        ains = [item for item in ains if item is not 'tmp.wav']
        ains.sort()
        for ain in ains:
            os.system('ffmpeg -y -loglevel error -i dataset/examples/{} -ar 16000 dataset/examples/tmp.wav'.format(ain))
            shutil.copyfile('dataset/examples/tmp.wav', 'dataset/examples/{}'.format(ain))

            # au embedding
            from talkingface.utils.thirdparty.resemblyer_util.speaker_emb import get_spk_emb
            me, ae = get_spk_emb('dataset/examples/{}'.format(ain))
            au_emb.append(me.reshape(-1))

            print('Processing audio file', ain)
            c = AutoVC_mel_Convertor('dataset/examples')

            au_data_i = c.convert_single_wav_to_autovc_input(audio_filename=os.path.join('dataset/examples', ain),
                   autovc_model_path=config['load_AUTOVC_name'])
            au_data += au_data_i
        if(os.path.isfile('dataset/examples/tmp.wav')):
            os.remove('dataset/examples/tmp.wav')

        # landmark fake placeholder
        fl_data = []
        rot_tran, rot_quat, anchor_t_shape = [], [], []
        for au, info in au_data:
            au_length = au.shape[0]
            fl = np.zeros(shape=(au_length, 68 * 3))
            fl_data.append((fl, info))
            rot_tran.append(np.zeros(shape=(au_length, 3, 4)))
            rot_quat.append(np.zeros(shape=(au_length, 4)))
            anchor_t_shape.append(np.zeros(shape=(au_length, 68 * 3)))

        if(os.path.exists(os.path.join('dataset/examples', 'dump', 'random_val_fl.pickle'))):
            os.remove(os.path.join('dataset/examples', 'dump', 'random_val_fl.pickle'))
        if(os.path.exists(os.path.join('dataset/examples', 'dump', 'random_val_fl_interp.pickle'))):
            os.remove(os.path.join('dataset/examples', 'dump', 'random_val_fl_interp.pickle'))
        if(os.path.exists(os.path.join('dataset/examples', 'dump', 'random_val_au.pickle'))):
            os.remove(os.path.join('dataset/examples', 'dump', 'random_val_au.pickle'))
        if (os.path.exists(os.path.join('dataset/examples', 'dump', 'random_val_gaze.pickle'))):
            os.remove(os.path.join('dataset/examples', 'dump', 'random_val_gaze.pickle'))

        with open(os.path.join('dataset/examples', 'dump', 'random_val_fl.pickle'), 'wb') as fp:
            pickle.dump(fl_data, fp)
        with open(os.path.join('dataset/examples', 'dump', 'random_val_au.pickle'), 'wb') as fp:
            pickle.dump(au_data, fp)
        with open(os.path.join('dataset/examples', 'dump', 'random_val_gaze.pickle'), 'wb') as fp:
            gaze = {'rot_trans':rot_tran, 'rot_quat':rot_quat, 'anchor_t_shape':anchor_t_shape}
            pickle.dump(gaze, fp)


        ''' STEP 4: RUN audio->landmark network'''
        model = Audio2landmark_model(opt_parser, jpg_shape=shape_3d)
        if(len(config['reuse_train_emb_list']) == 0):
            model.test(au_emb=au_emb)
        else:
            model.test(au_emb=None)


        ''' STEP 5: de-normalize the output to the original image scale '''
        fls = glob.glob1('dataset/examples', 'pred_fls_*.txt')
        fls.sort()

        for i in range(0,len(fls)):
            fl = np.loadtxt(os.path.join('dataset/examples', fls[i])).reshape((-1, 68,3))
            fl[:, :, 0:2] = -fl[:, :, 0:2]
            fl[:, :, 0:2] = fl[:, :, 0:2] / scale - shift

            if (ADD_NAIVE_EYE):
                fl = util.add_naive_eye(fl)

            # additional smooth
            fl = fl.reshape((-1, 204))
            fl[:, :48 * 3] = savgol_filter(fl[:, :48 * 3], 15, 3, axis=0)
            fl[:, 48*3:] = savgol_filter(fl[:, 48*3:], 5, 3, axis=0)
            fl = fl.reshape((-1, 68, 3))

            ''' STEP 6: Imag2image translation '''
            model = Image_translation_block(opt_parser, single_test=True)
            with torch.no_grad():
                model.single_test(jpg=img, fls=fl, filename=fls[i], prefix=config['jpg'].split('.')[0])
                print('finish image2image gen')
#             os.remove(os.path.join('dataset/examples', fls[i]))
        
        file_dict={"generated_video":[], "real_video":[]}
        file_dict['generated_video'].append("dataset/examples/generate.mp4")
        file_dict['real_video'].append("dataset/examples/groundtrue.mp4")

        return file_dict



class Embedder(nn.Module):
    def __init__(self, feat_size, d_model):
        super().__init__()
        self.embed = nn.Linear(feat_size, d_model)
    def forward(self, x):
        return self.embed(x)


class PositionalEncoder(nn.Module):
    def __init__(self, d_model, max_seq_len=512):
        super().__init__()
        self.d_model = d_model

        # create constant 'pe' matrix with values dependant on
        # pos and i
        pe = torch.zeros(max_seq_len, d_model)
        for pos in range(max_seq_len):
            for i in range(0, d_model, 2):
                pe[pos, i] = \
                    math.sin(pos / (10000 ** ((2 * i) / d_model)))
                pe[pos, i + 1] = \
                    math.cos(pos / (10000 ** ((2 * (i + 1)) / d_model)))

        pe = pe.unsqueeze(0)
        self.register_buffer('pe', pe)

    def forward(self, x):
        # make embeddings relatively larger
        x = x * math.sqrt(self.d_model)
        # add constant to embedding
        seq_len = x.size(1)
        x = x + self.pe[:, :seq_len].clone().detach().to(device)
        return x


def attention(q, k, v, d_k, mask=None, dropout=None):

    scores = torch.matmul(q, k.transpose(-2, -1)) / math.sqrt(d_k)
    if mask is not None:
        mask = mask.unsqueeze(1)
        scores = scores.masked_fill(mask == 0, -1e9)
    scores = F.softmax(scores, dim=-1)

    if dropout is not None:
        scores = dropout(scores)

    output = torch.matmul(scores, v)
    return output


class MultiHeadAttention(nn.Module):
    def __init__(self, heads, d_model, dropout=0.1):
        super().__init__()

        self.d_model = d_model
        self.d_k = d_model // heads
        self.h = heads

        self.q_linear = nn.Linear(d_model, d_model)
        self.v_linear = nn.Linear(d_model, d_model)
        self.k_linear = nn.Linear(d_model, d_model)
        self.dropout = nn.Dropout(dropout)
        self.out = nn.Linear(d_model, d_model)

    def forward(self, q, k, v, mask=None):
        bs = q.size(0)

        # perform linear operation and split into h heads

        k = self.k_linear(k).view(bs, -1, self.h, self.d_k)
        q = self.q_linear(q).view(bs, -1, self.h, self.d_k)
        v = self.v_linear(v).view(bs, -1, self.h, self.d_k)

        # transpose to get dimensions bs * h * sl * d_model

        k = k.transpose(1, 2)
        q = q.transpose(1, 2)
        v = v.transpose(1, 2)

        # calculate attention using function we will define next
        scores = attention(q, k, v, self.d_k, mask, self.dropout)

        # concatenate heads and put through final linear layer
        concat = scores.transpose(1, 2).contiguous() \
            .view(bs, -1, self.d_model)

        output = self.out(concat)

        return output
    
    def predict(self, audio_sequences, face_sequences):
        pass
    def calculate_loss(self, interaction, valid=False):
        pass
    def syncnet_loss(self, mel, g_frames):
        pass
    def cosine_loss(self, a, v, y):
        pass
    def load_syncnet(self):
        pass

    def generate_batch(self):
        pass

class FeedForward(nn.Module):
    def __init__(self, d_model, d_ff=2048, dropout = 0.1):
        super().__init__()
        # We set d_ff as a default to 2048
        self.linear_1 = nn.Linear(d_model, d_ff)
        self.dropout = nn.Dropout(dropout)
        self.linear_2 = nn.Linear(d_ff, d_model)
    def forward(self, x):
        x = self.dropout(F.relu(self.linear_1(x)))
        x = self.linear_2(x)
        return x


class Norm(nn.Module):
    def __init__(self, d_model, eps=1e-6):
        super().__init__()

        self.size = d_model
        # create two learnable parameters to calibrate normalisation
        self.alpha = nn.Parameter(torch.ones(self.size))
        self.bias = nn.Parameter(torch.zeros(self.size))
        self.eps = eps

    def forward(self, x):
        norm = self.alpha * (x - x.mean(dim=-1, keepdim=True)) \
               / (x.std(dim=-1, keepdim=True) + self.eps) + self.bias
        return norm

# build an encoder layer with one multi-head attention layer and one # feed-forward layer
class EncoderLayer(nn.Module):
    def __init__(self, d_model, heads, dropout=0.1):
        super().__init__()
        self.norm_1 = Norm(d_model)
        self.norm_2 = Norm(d_model)
        self.attn = MultiHeadAttention(heads, d_model)
        self.ff = FeedForward(d_model)
        self.dropout_1 = nn.Dropout(dropout)
        self.dropout_2 = nn.Dropout(dropout)

    def forward(self, x, mask):
        x2 = self.norm_1(x)
        x = x + self.dropout_1(self.attn(x2, x2, x2, mask))
        x2 = self.norm_2(x)
        x = x + self.dropout_2(self.ff(x2))
        return x

    # build a decoder layer with two multi-head attention layers and
    # one feed-forward layer
class DecoderLayer(nn.Module):
    def __init__(self, d_model, heads, dropout=0.1):
        super().__init__()
        self.norm_1 = Norm(d_model)
        self.norm_2 = Norm(d_model)
        self.norm_3 = Norm(d_model)

        self.dropout_1 = nn.Dropout(dropout)
        self.dropout_2 = nn.Dropout(dropout)
        self.dropout_3 = nn.Dropout(dropout)

        self.attn_1 = MultiHeadAttention(heads, d_model)
        self.attn_2 = MultiHeadAttention(heads, d_model)
        self.ff = FeedForward(d_model).cuda()

    def forward(self, x, e_outputs, src_mask, trg_mask):
        x2 = self.norm_1(x)

        x = x + self.dropout_1(self.attn_1(x2, x2, x2, trg_mask))
        x2 = self.norm_2(x)
        x = x + self.dropout_2(self.attn_2(x2, e_outputs, e_outputs, src_mask))
        x2 = self.norm_3(x)
        x = x + self.dropout_3(self.ff(x2))
        return x

    # We can then build a convenient cloning function that can generate multiple layers:
def get_clones(module, N):
    return nn.ModuleList([copy.deepcopy(module) for i in range(N)])


class Encoder(nn.Module):
    def __init__(self, d_model, N, heads, in_size):
        super().__init__()
        self.N = N
        self.embed = Embedder(in_size, d_model)
        self.pe = PositionalEncoder(d_model)
        self.layers = get_clones(EncoderLayer(d_model, heads), N)
        self.norm = Norm(d_model)

    def forward(self, x, mask=None):
        x = self.embed(x)
        x = self.pe(x)
        for i in range(self.N):
            x = self.layers[i](x, mask)
        return self.norm(x)


class Decoder(nn.Module):
    def __init__(self, d_model, N, heads, in_size):
        super().__init__()
        self.N = N
        self.embed = Embedder(in_size, d_model)
        self.pe = PositionalEncoder(d_model)
        self.layers = get_clones(DecoderLayer(d_model, heads), N)
        self.norm = Norm(d_model)

    def forward(self, x, e_outputs, src_mask=None, trg_mask=None):
        x = self.embed(x)
        x = self.pe(x)
        for i in range(self.N):
            x = self.layers[i](x, e_outputs, src_mask, trg_mask)
        return self.norm(x)


class Audio2landmark_pos(nn.Module):

    def __init__(self, audio_feat_size=80, c_enc_hidden_size=256, num_layers=3, drop_out=0,
                 spk_feat_size=256, spk_emb_enc_size=128, lstm_g_win_size=64, add_info_size=6,
                 transformer_d_model=32, N=2, heads=2, z_size=128, audio_dim=256):
        super(Audio2landmark_pos, self).__init__()

        self.lstm_g_win_size = lstm_g_win_size
        self.add_info_size = add_info_size
        comb_mlp_size = c_enc_hidden_size * 2

        self.audio_content_encoder = nn.LSTM(input_size=audio_feat_size,
                                             hidden_size=c_enc_hidden_size,
                                             num_layers=num_layers,
                                             dropout=drop_out,
                                             bidirectional=False,
                                             batch_first=True)

        self.use_audio_projection = not (audio_dim == c_enc_hidden_size)
        if(self.use_audio_projection):
            self.audio_projection = nn.Sequential(
                nn.Linear(in_features=c_enc_hidden_size, out_features=256),
                nn.LeakyReLU(0.02),
                nn.Linear(256, 128),
                nn.LeakyReLU(0.02),
                nn.Linear(128, audio_dim),
            )


        ''' original version '''
        self.spk_emb_encoder = nn.Sequential(
            nn.Linear(in_features=spk_feat_size, out_features=256),
            nn.LeakyReLU(0.02),
            nn.Linear(256, 128),
            nn.LeakyReLU(0.02),
            nn.Linear(128, spk_emb_enc_size),
        )
        # self.comb_mlp = nn.Sequential(
        #     nn.Linear(in_features=audio_dim + spk_emb_enc_size, out_features=comb_mlp_size),
        #     nn.LeakyReLU(0.02),
        #     nn.Linear(comb_mlp_size, comb_mlp_size // 2),
        #     nn.LeakyReLU(0.02),
        #     nn.Linear(comb_mlp_size // 2, 180),
        # )

        d_model = transformer_d_model * heads
        N = N
        heads = heads

        self.encoder = Encoder(d_model, N, heads, in_size=audio_dim + spk_emb_enc_size + z_size)
        self.decoder = Decoder(d_model, N, heads, in_size=204)
        self.out = nn.Sequential(
            nn.Linear(in_features=d_model + z_size, out_features=512),
            nn.LeakyReLU(0.02),
            nn.Linear(512, 256),
            nn.LeakyReLU(0.02),
            nn.Linear(256, 204),
        )


    def forward(self, au, emb, face_id, fls, z, add_z_spk=False, another_emb=None):

        # audio
        audio_encode, (_, _) = self.audio_content_encoder(au)
        audio_encode = audio_encode[:, -1, :]

        if(self.use_audio_projection):
            audio_encode = self.audio_projection(audio_encode)

        # spk
        spk_encode = self.spk_emb_encoder(emb)
        if(add_z_spk):
            z_spk = torch.tensor(torch.randn(spk_encode.shape)*0.01, requires_grad=False, dtype=torch.float).to(device)
            spk_encode = spk_encode + z_spk

        # comb
        # comb_input = torch.cat((audio_encode, spk_encode), dim=1)
        # comb_encode = self.comb_mlp(comb_input)
        comb_encode = torch.cat((audio_encode, spk_encode, z), dim=1)
        src_feat = comb_encode.unsqueeze(0)

        e_outputs = self.encoder(src_feat)[0]

        e_outputs = torch.cat((e_outputs, z), dim=1)

        fl_pred = self.out(e_outputs)

        return fl_pred, face_id[0:1, :], spk_encode




def nopeak_mask(size):
    np_mask = np.triu(np.ones((1, size, size)), k=1).astype('uint8')
    np_mask = torch.tensor(torch.from_numpy(np_mask) == 0)
    np_mask = np_mask.to(device)
    return np_mask


def create_masks(src, trg):
    src_mask = (src != torch.zeros_like(src, requires_grad=False))

    if trg is not None:
        size = trg.size(1)  # get seq_len for matrix
        np_mask = nopeak_mask(size)
        np_mask = np_mask.to(device)
        trg_mask = np_mask

    else:
        trg_mask = None
    return src_mask, trg_mask


class TalkingToon_spk2res_lstmgan_DL(nn.Module):
    def __init__(self, comb_emb_size=256, input_size=6):
        super(TalkingToon_spk2res_lstmgan_DL, self).__init__()

        self.fl_D = nn.Sequential(
            nn.Linear(in_features=FACE_ID_FEAT_SIZE, out_features=512),
            nn.LeakyReLU(0.02),
            nn.Linear(512, 256),
            nn.LeakyReLU(0.02),
            nn.Linear(256, 1),
        )

    def forward(self, feat):
        d = self.fl_D(feat)
        # d = torch.sigmoid(d)
        return d


class Transformer_DT(nn.Module):
    def __init__(self, transformer_d_model=32, N=2, heads=2, spk_emb_enc_size=128):
        super(Transformer_DT, self).__init__()
        d_model = transformer_d_model * heads
        self.encoder = Encoder(d_model, N, heads, in_size=204 + spk_emb_enc_size)
        self.out = nn.Sequential(
            nn.Linear(in_features=d_model, out_features=512),
            nn.LeakyReLU(0.02),
            nn.Linear(512, 256),
            nn.LeakyReLU(0.02),
            nn.Linear(256, 1),
        )

    def forward(self, fls, spk_emb, win_size=64, win_step=1):
        feat = torch.cat((fls, spk_emb), dim=1)

        win_size = feat.shape[0]-1 if feat.shape[0] <= win_size else win_size
        D_input = [feat[i:i+win_size:win_step] for i in range(0, feat.shape[0]-win_size)]
        D_input = torch.stack(D_input, dim=0)
        D_output = self.encoder(D_input)
        D_output = torch.max(D_output, dim=1, keepdim=False)[0]
        d = self.out(D_output)
        # d = torch.sigmoid(d)
        return d


class TalkingToon_spk2res_lstmgan_DT(nn.Module):
    def __init__(self, comb_emb_size=256, lstm_g_hidden_size=256, num_layers=3, drop_out=0, input_size=6):
        super(TalkingToon_spk2res_lstmgan_DT, self).__init__()

        self.fl_DT = nn.GRU(input_size=comb_emb_size + FACE_ID_FEAT_SIZE,
                                             hidden_size=lstm_g_hidden_size,
                                             num_layers=3,
                                             dropout=0,
                                             bidirectional=False,
                                             batch_first=True)
        self.projection = nn.Sequential(
            nn.Linear(in_features=lstm_g_hidden_size, out_features=512),
            nn.LeakyReLU(0.02),
            nn.Linear(512, 256),
            nn.LeakyReLU(0.02),
            nn.Linear(256, 1),
        )

        self.maxpool = nn.MaxPool1d(4, 1)

    def forward(self, comb_encode, fls, win_size=32, win_step=1):
        feat = torch.cat((comb_encode, fls), dim=1)
        # v
        # feat = torch.cat((comb_encode[0:-1], fls[1:] - fls[0:-1]), dim=1)

        # max pooling
        feat = feat.transpose(0, 1).unsqueeze(0)
        feat = self.maxpool(feat)
        feat = feat[0].transpose(0, 1)

        win_size = feat.shape[0] - 1 if feat.shape[0] <= win_size else win_size
        D_input = [feat[i:i+win_size:win_step] for i in range(0, feat.shape[0]-win_size)]
        D_input = torch.stack(D_input, dim=0)
        D_output, _ = self.fl_DT(D_input)
        D_output = D_output[:, -1, :]
        d = self.projection(D_output)
        # d = torch.sigmoid(d)
        return d