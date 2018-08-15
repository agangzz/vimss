from sacred import Experiment
import pickle
import numpy as np
import tensorflow as tf
import librosa
import soundfile as sf
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
from mpl_toolkits.axes_grid1 import make_axes_locatable

import os
import json
import glob

from Input import Input
from Input import urmp_input
import Models.UnetAudioSeparator

import musdb
import museval
import mir_eval

ex = Experiment('Evaluate-Metrics')

@ex.config
def cfg():
    # Base configuration
    config = {
        'data_path': 'gs://vimsstfrecords/urmp-labels',
        'estimates_path': 'estimates',
        'experiment_id': np.random.randint(0, 1000000)
    }


def alpha_snr(target, estimate):
    # Compute SNR: 10 log_10 ( ||s_target||^2 / ||s_target - alpha * s_estimate||^2 ), but scale target to get optimal SNR (opt. wrt. alpha)
    # Optimal alpha is Sum_i=1(s_target_i * s_estimate_i) / Sum_i=1 (s_estimate_i ^ 2) = inner_prod / estimate_power
    estimate_power = np.sum(np.square(estimate))
    target_power = np.sum(np.square(target))
    inner_prod = np.inner(estimate, target)
    alpha = inner_prod / estimate_power
    error_power = np.sum(np.square(target - alpha * estimate))
    snr = 10 * np.log10(target_power / error_power)
    return snr

def predict(track):
    '''
    Function in accordance with MUSB evaluation API. Takes MUSDB track object and computes corresponding source estimates, as well as calls evlauation script.
    Model has to be saved beforehand into a pickle file containing model configuration dictionary and checkpoint path!
    :param track: Track object
    :return: Source estimates dictionary
    '''
    '''if track.filename[:4] == "test" or int(track.filename[:3]) > 53:
        return {
            'vocals': np.zeros(track.audio.shape),
            'accompaniment': np.zeros(track.audio.shape)
        }'''
    # Load model hyper-parameters and model checkpoint path
    with open("prediction_params.pkl", "r") as file:
        [model_config, load_model] = pickle.load(file)

    # Determine input and output shapes, if we use U-net as separator
    disc_input_shape = [model_config["batch_size"], model_config["num_frames"], 0]  # Shape of discriminator input
    if model_config["network"] == "unet":
        separator_class = Models.UnetAudioSeparator.UnetAudioSeparator(model_config["num_layers"], model_config["num_initial_filters"],
                                                                   output_type=model_config["output_type"],
                                                                   context=model_config["context"],
                                                                   mono=model_config["mono_downmix"],
                                                                   upsampling=model_config["upsampling"],
                                                                   num_sources=model_config["num_sources"],
                                                                   filter_size=model_config["filter_size"],
                                                                   merge_filter_size=model_config["merge_filter_size"])

    sep_input_shape, sep_output_shape = separator_class.get_padding(np.array(disc_input_shape))
    separator_func = separator_class.get_output

    # Batch size of 1
    sep_input_shape[0] = 1
    sep_output_shape[0] = 1

    mix_context, sources = Input.get_multitrack_placeholders(sep_output_shape, model_config["num_sources"], sep_input_shape, "input")

    print("Testing...")

    # BUILD MODELS
    # Separator
    separator_sources = separator_func(mix_context, False, reuse=False)

    # Start session and queue input threads
    sess = tf.Session()
    sess.run(tf.global_variables_initializer())

    # Load model
    # Load pretrained model to continue training, if we are supposed to
    restorer = tf.train.Saver(None, write_version=tf.train.SaverDef.V2)
    print("Num of variables" + str(len(tf.global_variables())))
    restorer.restore(sess, load_model)
    print('Pre-trained model restored for song prediction')

    mix_audio, orig_sr, mix_channels = track.audio, track.rate, track.audio.shape[1] # Audio has (n_samples, n_channels) shape
    separator_preds = predict_track(model_config, sess, mix_audio, orig_sr, sep_input_shape, sep_output_shape, separator_sources, mix_context)

    # Upsample predicted source audio and convert to stereo
    pred_audio = [librosa.resample(pred.T, model_config["expected_sr"], orig_sr).T for pred in separator_preds]

    if model_config["mono_downmix"] and mix_channels > 1: # Convert to multichannel if mixture input was multichannel by duplicating mono estimate
        pred_audio = [np.tile(pred, [1, mix_channels]) for pred in pred_audio]

    # Set estimates depending on estimation task (voice or multi-instrument separation)
    if model_config["task"] == "voice": # [acc, vocals] order
        estimates = {
            'vocals' : pred_audio[1],
            'accompaniment' : pred_audio[0]
        }
    else: # [bass, drums, other, vocals]
        estimates = {
            'bass' : pred_audio[0],
            'drums' : pred_audio[1],
            'other' : pred_audio[2],
            'vocals' : pred_audio[3]
        }

    # Evaluate using museval
    scores = museval.eval_mus_track(
        track, estimates, output_dir="/mnt/daten/Datasets/MUSDB18/eval", # SiSec should use longer win and hop parameters here to make evaluation more stable!
    )

    # print nicely formatted mean scores
    print(scores)

    # Close session, clear computational graph
    sess.close()
    tf.reset_default_graph()

    return estimates

def predict_track(model_config, sess, mix_audio, mix_sr, sep_input_shape, sep_output_shape, separator_sources, mix_context):
    '''
    Outputs source estimates for a given input mixture signal mix_audio [n_frames, n_channels] and a given Tensorflow session and placeholders belonging to the prediction network.
    It iterates through the track, collecting segment-wise predictions to form the output.
    :param model_config: Model configuration dictionary
    :param sess: Tensorflow session used to run the network inference
    :param mix_audio: [n_frames, n_channels] audio signal (numpy array). Can have higher sampling rate or channels than the model supports, will be downsampled correspondingly.
    :param mix_sr: Sampling rate of mix_audio
    :param sep_input_shape: Input shape of separator ([batch_size, num_samples, num_channels])
    :param sep_output_shape: Input shape of separator ([batch_size, num_samples, num_channels])
    :param separator_sources: List of Tensorflow tensors that represent the output of the separator network
    :param mix_context: Input tensor of the network
    :return: 
    '''
    # Load mixture, convert to mono and downsample then
    assert(len(mix_audio.shape) == 2)
    if model_config["mono_downmix"]:
        mix_audio = np.mean(mix_audio, axis=1, keepdims=True)
    else:
        if mix_audio.shape[1] == 1:# Duplicate channels if input is mono but model is stereo
            mix_audio = np.tile(mix_audio, [1, 2])
    mix_audio = librosa.resample(mix_audio.T, mix_sr, model_config["expected_sr"], res_type="kaiser_fast").T

    # Preallocate source predictions (same shape as input mixture)
    source_time_frames = mix_audio.shape[0]
    source_preds = [np.zeros(mix_audio.shape, np.float32) for _ in range(model_config["num_sources"])]

    input_time_frames = sep_input_shape[1]
    output_time_frames = sep_output_shape[1]

    # Pad mixture across time at beginning and end so that neural network can make prediction at the beginning and end of signal
    pad_time_frames = (input_time_frames - output_time_frames) / 2
    mix_audio_padded = np.pad(mix_audio, [(pad_time_frames, pad_time_frames), (0,0)], mode="constant", constant_values=0.0)

    # Iterate over mixture magnitudes, fetch network rpediction
    for source_pos in range(0, source_time_frames, output_time_frames):
        # If this output patch would reach over the end of the source spectrogram, set it so we predict the very end of the output, then stop
        if source_pos + output_time_frames > source_time_frames:
            source_pos = source_time_frames - output_time_frames

        # Prepare mixture excerpt by selecting time interval
        mix_part = mix_audio_padded[source_pos:source_pos + input_time_frames,:]
        mix_part = np.expand_dims(mix_part, axis=0)

        source_parts = sess.run(separator_sources, feed_dict={mix_context: mix_part})

        # Save predictions
        # source_shape = [1, freq_bins, acc_mag_part.shape[2], num_chan]
        for i in range(model_config["num_sources"]):
            source_preds[i][source_pos:source_pos + output_time_frames] = source_parts[i][0, :, :]

    return source_preds


def compute_mean_metrics(json_folder, compute_averages=True):
    files = glob.glob(os.path.join(json_folder, "*.json"))
    sdr_inst_list = None
    for path in files:
        #print(path)
        with open(path, "r") as f:
            js = json.load(f)

        if sdr_inst_list is None:
            sdr_inst_list = [list() for _ in range(len(js["targets"]))]

        for i in range(len(js["targets"])):
            sdr_inst_list[i].extend([np.float(f['metrics']["SDR"]) for f in js["targets"][i]["frames"]])

    #return np.array(sdr_acc), np.array(sdr_voc)
    sdr_inst_list = [np.array(sdr) for sdr in sdr_inst_list]

    if compute_averages:
        return [(np.nanmedian(sdr), np.nanmedian(np.abs(sdr - np.nanmedian(sdr))), np.nanmean(sdr), np.nanstd(sdr)) for sdr in sdr_inst_list]
    else:
        return sdr_inst_list

def draw_violin_sdr(json_folder):
    acc, voc = compute_mean_metrics(json_folder, compute_averages=False)
    acc = acc[~np.isnan(acc)]
    voc = voc[~np.isnan(voc)]
    data = [acc, voc]
    inds = [1,2]

    fig, ax = plt.subplots()
    ax.violinplot(data, showmeans=True, showmedians=False, showextrema=False, vert=False)
    ax.scatter(np.percentile(data, 50, axis=1),inds, marker="o", color="black")
    ax.set_title("Segment-wise SDR distribution")
    ax.vlines([np.min(acc), np.min(voc), np.max(acc), np.max(voc)], [0.8, 1.8, 0.8, 1.8], [1.2, 2.2, 1.2, 2.2], color="blue")
    ax.hlines(inds, [np.min(acc), np.min(voc)], [np.max(acc), np.max(voc)], color='black', linestyle='--', lw=1, alpha=0.5)

    ax.set_yticks([1,2])
    ax.set_yticklabels(["Accompaniment", "Vocals"])

    fig.set_size_inches(8, 3.)
    fig.savefig("sdr_histogram.pdf", bbox_inches='tight')

@ex.automain
def compute_metrics(config):
    sess = tf.Session()

    (SDR, SIR, SAR) = list(range(3))
    metrics = [list(), list(), list()]

    urmp_test = urmp_input.URMPInput(
        mode='test',
        data_dir=config['data_path'],
        transpose_input=False,
        use_bfloat16=False).input_fn({'batch_size': 1})
    data_item = urmp_test.make_one_shot_iterator().get_next()

    # evaluate until StopIteration error
    try:
        while True:
            data_value = sess.run(data_item)
            gt_features, gt_sources = data_value[0], data_value[1]

            est_sources = np.zeros(shape=gt_sources.shape)

            # lookup estimated sources and load them
            est_base_dirname = config['estimates_path'] + os.path.sep + urmp_input.BASENAMES[gt_features['filename']]
            inv_source_map = {v: k for k, v in urmp_input.SOURCE_MAP.iteritems()}
            for source_id in range(len(gt_sources)):
                source_name = inv_source_map[source_id+1]
                est_source_filename = os.path.join(est_base_dirname, source_name, str(gt_features['sample_id'])+'.wav')
                est_sources[source_id, :] = sf.read(est_source_filename)[0]

            # compute mir_eval separation metrics
            (sdr, sir, sar, _) = mir_eval.separation.bss_eval_sources(gt_sources, est_sources, compute_permutation=False)
            metrics[SDR].append(sdr)
            metrics[SIR].append(sir)
            metrics[SAR].append(sar)

    except StopIteration:
        print("Metrics computed")

    print("Mean SDR: ", np.mean(metrics[SDR]))
    print("Mean SIR: ", np.mean(metrics[SIR]))
    print("Mean SAR: ", np.mean(metrics[SAR]))

    metrics_data_path = os.path.join(config['estimates_path'], 'metrics.npy')
    np.save(metrics_data_path, np.array(metrics))
    print("Metrics saved at ", metrics_data_path)
