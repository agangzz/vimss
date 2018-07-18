import os.path

import Input.Input
from Sample import Sample
import csv

import numpy as np
from lxml import etree
import librosa
import soundfile
import os
import fnmatch
from exceptions import Exception
import musdb


def subtract_audio(mix_list, instrument_list):
    '''
    Generates new audio by subtracting the audio signal of an instrument recording from a mixture
    :param mix_list:
    :param instrument_list:
    :return:
    '''

    assert(len(mix_list) == len(instrument_list))
    new_audio_list = list()

    for i in range(0, len(mix_list)):
        new_audio_path = os.path.dirname(mix_list[i]) + os.path.sep + "remainingmix" + os.path.splitext(mix_list[i])[1]
        new_audio_list.append(new_audio_path)

        if os.path.exists(new_audio_path):
            continue
        mix_audio, mix_sr = librosa.load(mix_list[i], mono=False, sr=None)
        inst_audio, inst_sr = librosa.load(instrument_list[i], mono=False, sr=None)
        assert (mix_sr == inst_sr)
        new_audio = mix_audio - inst_audio
        if not (np.min(new_audio) >= -1.0 and np.max(new_audio) <= 1.0):
            print("Warning: Audio for mix " + str(new_audio_path) + " exceeds [-1,1] float range!")

        librosa.output.write_wav(new_audio_path, new_audio, mix_sr) #TODO switch to compressed writing
        print("Wrote accompaniment for song " + mix_list[i])
    return new_audio_list

def create_sample(db_path, instrument_node):
   path = db_path + os.path.sep + instrument_node.xpath("./relativeFilepath")[0].text
   sample_rate = int(instrument_node.xpath("./sampleRate")[0].text)
   channels = int(instrument_node.xpath("./numChannels")[0].text)
   duration = float(instrument_node.xpath("./length")[0].text)
   return Sample(path, sample_rate, channels, duration)

def getAllFilesOfType(root_path, extension):
    matches = []
    for root, dirnames, filenames in os.walk(root_path):
        for filename in fnmatch.filter(filenames, '*.' + extension):
            matches.append(os.path.join(root, filename))
    return matches

def get_samples_in_folder(audio_path, extension):
    audio_file_list = getAllFilesOfType(audio_path, extension)
    sample_list = list()
    for audio_file in audio_file_list:
        print("Reading in metadata of file " + audio_file)
        try:
            sample = Sample.from_path(audio_file)
        except Exception:
            print("Skipping sample at path " + audio_path)
            continue
        sample_list.append(sample)
    assert(len(sample_list) > 0) # If we did not find any samples something must have gone wrong
    return sample_list

def getFullFMA(audio_path):
    return get_samples_in_folder(audio_path, "mp3")

def getLooperman(audio_path):
    return get_samples_in_folder(audio_path, "mp3")

def getPopFMA(database_path, audio_path=None):
    return getFMAGenre(10, database_path, audio_path)

def getVocalFMA(database_path, audio_path=None):
    if audio_path is None:
        audio_path = database_path

    track_csv = os.path.join(database_path, "fma_metadata", "raw_tracks.csv")
    sample_list = list()

    with open(track_csv, 'rb') as csvfile:
        reader = csv.DictReader(csvfile)
        for row in reader:
            if not int(row["track_instrumental"]):
                track_id = int(row["track_id"])
                filename = '{:06d}'.format(track_id) + ".mp3"
                folder_id = track_id // 1000
                foldername = '{:03d}'.format(folder_id)
                audio_file = os.path.join(audio_path, "fma_full", foldername, filename)
                print("Reading in metadata of file " + audio_file)
                try:
                    sample = Sample.from_path(audio_file)
                except Exception as e:
                    print("Skipping sample at path " + audio_file)
                    print(e)
                    continue
                sample_list.append(sample)
    return sample_list

def convert_float_to_pcm(float_audio):
    assert(isinstance(float_audio, np.ndarray))
    assert(float_audio.dtype == np.float32 or float_audio.dtype == np.float64)
    return (float_audio * 32767).astype(np.int16)

def write_wav_skip_existing(path, y, sr, norm=False):
    if not os.path.exists(path):
        soundfile.write(path, y, sr, "PCM_16")
    else:
        print("WARNING: Tried writing audio to " + path + ", but audio file exists already. Skipping file!")

def getMUSDB(database_path):
    mus = musdb.DB(root_dir=database_path, is_wav=False)

    subsets = list()

    for subset in ["train", "test"]:
        tracks = mus.load_mus_tracks(subset)
        samples = list()

        for track in tracks:
            # Get mix and instruments
            # Bass
            bass_path = track.path[:-4] + "_bass.wav"
            bass_audio, bass_rate = soundfile.read(bass_path, always_2d=True)
            bass_duration = bass_audio.shape[0] / bass_rate
            bass = Sample(bass_path, bass_rate, bass_audio.shape[1], bass_duration)

            # Drums
            drums_path = track.path[:-4] + "_drums.wav"
            drum_audio, drum_rate = soundfile.read(drums_path, always_2d=True)
            drum_duration = drum_audio.shape[0] / drum_rate
            drums = Sample(drums_path, drum_rate, drum_audio.shape[1], drum_duration)

            # Other
            other_path = track.path[:-4] + "_other.wav"
            other_audio, other_rate = soundfile.read(other_path, always_2d=True)
            other_duration = other_audio.shape[0] / other_rate
            other = Sample(other_path, other_rate, other_audio.shape[1], other_duration)

            # Vocals
            vocal_path = track.path[:-4] + "_vocals.wav"
            vocal_audio, vocal_rate = soundfile.read(vocal_path, always_2d=True)
            vocal_duration = vocal_audio.shape[0] / vocal_rate
            vocal = Sample(vocal_path, vocal_rate, vocal_audio.shape[1], vocal_duration)

            # Add other instruments to form accompaniment
            #acc_audio = track.sources["drums"].audio + track.sources["bass"].audio + track.sources["other"].audio
            acc_audio = drum_audio + bass_audio + other_audio
            acc_path = track.path[:-4] + "_accompaniment.wav"
            acc = Sample(acc_path, bass_rate, acc_audio.shape[1], drum_duration)

            # Create mixture
            mix_path = track.path[:-4] + "_mix.wav"
            mix_audio, mix_rate = soundfile.read(mix_path, always_2d=True)
            mix_duration = mix_audio.shape[0] / mix_rate
            mix = Sample(mix_path, mix_rate, mix_audio.shape[1], mix_duration)

            diff_signal = np.abs(mix_audio - bass_audio - drum_audio - other_audio - vocal_audio)
            print("Maximum absolute deviation from source additivity constraint: " + str(np.max(diff_signal)))# Check if acc+vocals=mix
            print("Mean absolute deviation from source additivity constraint:    " + str(np.mean(diff_signal)))

            samples.append((mix, acc, bass, drums, other, vocal)) # Collect all sources for now. Later on for SVS: [mix, acc, vocal] Multi-instrument: [mix, bass, drums, other, vocals]

        subsets.append(samples)

    return subsets

def getURMPV2(database_path):

    # Silent file used for instrument sources not present in the file
    silence_path='/mnt/disks/datasets/silence.wav'

    # Instrument source to index mapping
    source_map = {
        'mix': 0,
        'bn': 1,
        'cl': 2,
        'db': 3,
        'fl': 4,
        'hn': 5,
        'ob': 6,
        'sax': 7,
        'tba': 8,
        'tbn': 9,
        'tpt': 10,
        'va': 11,
        'vc': 12,
        'vn': 13,
    }

    subsets = []
    for subset in ["train", "test"]:
        subset_dir = os.path.join(database_path, subset)
        track_list = []

        # Iterate through each tracks
        for folder in os.listdir(subset_dir):
            track_sources = [0 for i in range(14)]  # 1st index must be mix source + 13 individual sources

            # Create Sample object for each instrument source files present
            for filename in os.listdir(os.path.join(subset_dir, folder)):
                if filename.endswith(".wav"):
                    if filename.startswith("AuMix"):
                        # Place mix source to the first index
                        mix_path = os.path.join(subset_dir, folder, filename)
                        mix_audio, mix_rate = soundfile.read(mix_path, always_2d=True)
                        mix_duration = mix_audio.shape[0] / mix_rate
                        mix = Sample(mix_path, mix_rate, mix_audio.shape[1], mix_duration)
                        track_sources.insert(0, mix)
                    else:
                        # Place Sample object mapping to its instrument index
                        source_name = filename.split('_')[2]
                        source_idx = source_map[source_name]
                        source_path = os.path.join(subset_dir, folder, filename)
                        source_audio, source_rate = soundfile.read(source_path, always_2d=True)
                        source_duration = source_audio.shape[0] / source_rate
                        source = Sample(source_path, source_rate, source_audio.shape[1], source_duration)
                        track_sources.insert(source_idx, source)

            # Create and insert silence Sample object for instruments not present in the track
            silence_audio, silence_rate = soundfile.read(silence_path, always_2d=True)
            silence_duration = silence_audio.shape[0] / silence_rate
            silence = Sample(silence_path, silence_rate, silence_audio.shape[1], silence_duration)

            for i, track in enumerate(track_sources):
                if track == 0:
                    track_sources[i] = silence

            # Cast to tuple to match the dataset.pkl format
            track_list.append(tuple(track_sources))
        subsets.append(track_list)

    return subsets


def getFMAGenre(genre_id, database_path, audio_path=None):
    if audio_path is None:
        audio_path = database_path

    track_csv = os.path.join(database_path, "fma_metadata", "raw_tracks.csv")
    sample_list = list()

    with open(track_csv, 'rb') as csvfile:
        reader = csv.DictReader(csvfile)
        for row in reader:
            if row["track_genres"].__contains__("genre_id': '" + str(genre_id) + "'"):
                track_id = int(row["track_id"])
                filename = '{:06d}'.format(track_id) + ".mp3"
                folder_id = track_id // 1000
                foldername = '{:03d}'.format(folder_id)
                audio_file = os.path.join(audio_path, "fma_full", foldername, filename)
                print("Reading in metadata of file " + audio_file)
                try:
                     sample = Sample.from_path(audio_file)
                except Exception as e:
                    print("Skipping sample at path " + audio_file)
                    print(e)
                    continue
                sample_list.append(sample)
    return sample_list

def getDSDFilelist(xml_path):
    tree = etree.parse(xml_path)
    root = tree.getroot()
    db_path = root.find("./databaseFolderPath").text
    tracks = root.findall(".//track")

    train_vocals, test_vocals, train_mixes, test_mixes, train_accs, test_accs = list(), list(), list(), list(), list(), list()

    for track in tracks:
        # Get mix and vocal instruments
        vocals = create_sample(db_path, track.xpath(".//instrument[instrumentName='Voice']")[0])
        mix = create_sample(db_path, track.xpath(".//instrument[instrumentName='Mix']")[0])
        [acc_path] = subtract_audio([mix.path], [vocals.path])
        acc = Sample(acc_path, vocals.sample_rate, vocals.channels, vocals.duration) # Accompaniment has same signal properties as vocals and mix

        if track.xpath("./databaseSplit")[0].text == "Training":
            train_vocals.append(vocals)
            train_mixes.append(mix)
            train_accs.append(acc)
        else:
            test_vocals.append(vocals)
            test_mixes.append(mix)
            test_accs.append(acc)

    return [train_mixes, train_accs, train_vocals], [test_mixes, test_accs, test_vocals]

def getCCMixter(xml_path):
    tree = etree.parse(xml_path)
    root = tree.getroot()
    db_path = root.find("./databaseFolderPath").text
    tracks = root.findall(".//track")

    samples = list()

    for track in tracks:
        # Get mix and vocal instruments
        voice = create_sample(db_path, track.xpath(".//instrument[instrumentName='Voice']")[0])
        mix = create_sample(db_path, track.xpath(".//instrument[instrumentName='Mix']")[0])
        acc = create_sample(db_path, track.xpath(".//instrument[instrumentName='Instrumental']")[0])

        samples.append((mix, acc, voice))

    return samples

def getIKala(xml_path):
    tree = etree.parse(xml_path)
    root = tree.getroot()
    db_path = root.find("./databaseFolderPath").text
    tracks = root.findall(".//track")

    mixes, accs, vocals = list(), list(), list()

    for track in tracks:
        mix = create_sample(db_path, track.xpath(".//instrument[instrumentName='Mix']")[0])
        orig_path = mix.path
        mix_path = orig_path + "_mix.wav"
        acc_path = orig_path + "_acc.wav"
        voice_path = orig_path + "_voice.wav"

        mix_audio, mix_sr = librosa.load(mix.path, sr=None, mono=False)
        mix.path = mix_path
        librosa.output.write_wav(mix_path, np.sum(mix_audio, axis=0), mix_sr)
        librosa.output.write_wav(acc_path, mix_audio[0,:], mix_sr)
        librosa.output.write_wav(voice_path, mix_audio[1, :], mix_sr)

        voice = create_sample(mix.path, track.xpath(".//instrument[instrumentName='Voice']")[0])
        voice.path = voice_path
        acc = create_sample(mix.path, track.xpath(".//instrument[instrumentName='Instrumental']")[0])
        acc.path = acc_path

        mixes.append(mix)
        accs.append(acc)
        vocals.append(voice)

    return [mixes, accs, vocals]

def getMedleyDB(xml_path):
    tree = etree.parse(xml_path)
    root = tree.getroot()
    db_path = root.find("./databaseFolderPath").text

    mixes, accs, vocals = list(), list(), list()

    tracks = root.xpath(".//track")
    for track in tracks:
        instrument_paths = list()
        # Mix together vocals, if they exist
        vocal_tracks = track.xpath(".//instrument[instrumentName='Voice']/relativeFilepath") + \
                       track.xpath(".//instrument[instrumentName='Voice']/relativeFilepath") + \
                       track.xpath(".//instrument[instrumentName='Voice']/relativeFilepath")
        if len(vocal_tracks) > 0: # If there are vocals, get their file paths and mix them together
            vocal_track = Input.Input.add_audio([db_path + os.path.sep + f.text for f in vocal_tracks], "vocalmix")
            instrument_paths.append(vocal_track)
            vocals.append(Sample.from_path(vocal_track))
        else: # Otherwise append duration of track so silent input can be generated later on-the-fly
            duration = float(track.xpath("./instrumentList/instrument/length")[0].text)
            vocals.append(duration)

        # Mix together accompaniment, if it exists
        acc_tracks = track.xpath(".//instrument[not(instrumentName='Voice') and not(instrumentName='Mix') and not(instrumentName='Instrumental')]/relativeFilepath") #TODO # We assume that there is no distinction between male/female here
        if len(acc_tracks) > 0:  # If there are vocals, get their file paths and mix them together
            acc_track = Input.Input.add_audio([db_path + os.path.sep + f.text for f in acc_tracks], "accmix")
            instrument_paths.append(acc_track)
            accs.append(Sample.from_path(acc_track))
        else:  # Otherwise append duration of track so silent input can be generated later on-the-fly
            duration = float(track.xpath("./instrumentList/instrument/length")[0].text)
            accs.append(duration)

        # Mix together vocals and accompaniment
        mix_track = Input.Input.add_audio(instrument_paths, "totalmix")
        mixes.append(Sample.from_path(mix_track))

    return [mixes, accs, vocals]

def getFMA(xml_path):
    tree = etree.parse(xml_path)
    root = tree.getroot()
    db_path = root.find("./databaseFolderPath").text

    mixes, accs, vocals = list(), list(), list()

    vocal_tracks = root.xpath(".//track/instrumentList/instrument[instrumentName='Mix']")
    instrumental_tracks = root.xpath(".//track/instrumentList/instrument[instrumentName='Instrumental']")
    for instr in vocal_tracks:
        mixes.append(create_sample(db_path,instr))

    for instr in instrumental_tracks:
        mixes.append(create_sample(db_path,instr))
        accs.append(create_sample(db_path,instr))

    return mixes, accs, vocals