"""
Wrappers around cmlreaders and PTSA to load events, electrode information, eeg data, and to compute power with wavelets.
This is a bit more high level and more geared towards helping users do some commonly performed tasks with the data.
"""

import numexpr
import os
import warnings
import numpy as np
import pandas as pd
import xarray as xr

from ptsa.data.filters import ButterworthFilter
from ptsa.data.filters import MorletWaveletFilter
from ptsa.data.filters import ResampleFilter
from ptsa.data.timeseries import TimeSeries

from cmlreaders import CMLReader, get_data_index
from scipy.stats.mstats import zscore
from scipy.io import loadmat
from tqdm import tqdm
from glob import glob

# get the r1 dataframe on import so we don't have to keep doing it
try:
    r1_data = get_data_index("r1")
except KeyError:
    print('r1 protocol file not found')


def get_subjs_and_montages(task):
    """Returns a DataFrame with columns 'subject' and 'montage' listing participants in a given experiment.

    Parameters
    ----------
    task: str
        The experiment name (ex: TH1, FR1, ...).

    Returns
    -------
    pandas.DataFrame
        A DataFrame of all subjects who performed the task.
    """

    # if this is RAM task, load the subject/montage directly from the r1 database
    task = task.replace('RAM_', '')
    if task in r1_data.experiment.unique():
        df = r1_data[r1_data['experiment'] == task][['subject', 'montage']].drop_duplicates().reset_index(drop=True)

    # otherwise, need to look for *events.mat files in '/data/events/task
    else:
        subj_list = []
        mont_list = []
        subjs = glob(os.path.join('/data/events/', task, '*_events.mat'))
        subjs = [os.path.split(f.replace('_events.mat', ''))[1] for f in subjs]
        subjs.sort()
        for subj in subjs:
            m = 0
            if '_' in subj:
                subj_split = subj.split('_')
                if len(subj_split[-1]) == 1:
                    m = int(subj_split[-1])
                    subj = subj[:-2]
            subj_list.append(subj)
            mont_list.append(m)
        df = pd.DataFrame({'subject': np.array(subj_list, dtype=object), 'montage': np.array(mont_list, dtype=int)})
    return df


def load_subj_events(task, subject, montage, as_df=True):
    """Returns a DataFrame of the events.

    Parameters
    ----------
    task: str
        The experiment name (ex: RAM_TH1, RAM_FR1, ...).
    subject: str
        The subject code
    montage: int
        The montage number for the subject
    as_df: bool
        If true, the events will returned as a pandas.DataFrame, otherwise a numpy.recarray

    Returns
    -------
    pandas.DataFrame
        A DataFrame of of the events
    """
    task = task.replace('RAM_', '')

    # if a RAM task, get info from r1 database and load as df using cmlreader
    if task in r1_data.experiment.unique():
        # get list of sessions for this subject, experiment, montage
        inds = (r1_data['subject'] == subject) & (r1_data['experiment'] == task) & (r1_data['montage'] == int(montage))
        sessions = r1_data[inds]['session'].unique()

        # load all and concat
        events = pd.concat([CMLReader(subject=subject,
                                      experiment=task,
                                      session=session).load('events')
                            for session in sessions])
        if not as_df:
            events = events.to_records(index=False)

    # otherwise load matlab files
    else:
        subj_file = subject + '_events.mat'
        if int(montage) != 0:
            subj_file = subject + '_' + str(montage) + '_events.mat'
        subj_ev_path = str(os.path.join('/data/events/', task, subj_file))
        events = loadmat(subj_ev_path, squeeze_me=True)['events']
        events.dtype.names = ['item_name' if i == 'item' else i for i in events.dtype.names]

        if as_df:
            events = pd.DataFrame.from_records(events)

    return events


def load_elec_info(subject, montage=0, bipolar=True):
    """

    Parameters
    ----------
    subject: str
        subject code
    montage: int
        montage number
    bipolar: bool
        whether to return electrode info for bipolar or monopolar electrode configuration

    Returns
    -------
    pandas.DataFrame
        A DataFrame of of the electrode information

    """

    # check if this subject/montage is in r1. If it is, use cmlreaders to load it. Easy.
    if np.any((r1_data['subject'] == subject) & (r1_data['montage'] == montage)):
        elec_df = CMLReader(subject=subject, montage=montage).load('pairs' if bipolar else 'contacts')

    # otherwise, load the mat file and do some reorganization to make it a nice dataframe
    else:
        # load appropriate .mat file
        subj_mont = subject
        if int(montage) != 0:
            subj_mont = subject + '_' + str(montage)
        file_str = '_bipol' if bipolar else ''
        tal_path = os.path.join('/data/eeg', subj_mont, 'tal', subj_mont + '_talLocs_database' + file_str + '.mat')
        elec_raw = loadmat(tal_path, squeeze_me=True)
        elec_raw = elec_raw[np.setdiff1d(list(elec_raw.keys()), ['__header__', '__version__', '__globals__'])[0]]

        # sume of the data is in subarrays, flatten it, and make dataframe. Eeessh
        # also rename some of the fields/columns
        # make average surface dataframe
        surf_data = []
        exclude = []
        if 'avgSurf' in elec_raw.dtype.names:
            avg_surf = pd.concat([pd.DataFrame(index=[i], data=e) for (i, e) in enumerate(elec_raw['avgSurf'])],
                                 sort=False)
            avg_surf = avg_surf.rename(columns={x: 'avg.{}'.format(x) for x in avg_surf.columns})
            surf_data.append(avg_surf)
            exclude.append('avgSurf')

        # make indiv surface dataframe
        if 'indivSurf' in elec_raw.dtype.names:
            ind_surf = pd.concat([pd.DataFrame(index=[i], data=e) for (i, e) in enumerate(elec_raw['indivSurf'])],
                                 sort=False)
            ind_surf = ind_surf.rename(columns={x: 'ind.{}'.format(x) for x in ind_surf.columns})
            surf_data.append(ind_surf)
            exclude.append('indivSurf')

        # concat them, excluding the original subarrays
        elec_df = pd.DataFrame.from_records(elec_raw, exclude=exclude)
        elec_df = pd.concat([elec_df] + surf_data, axis='columns')

        # add new columns for contacts, named the same as the json version
        if bipolar:
            elec_df['contact_1'], elec_df['contact_2'] = np.stack(elec_df['channel'], -1)
            elec_df.drop(columns='channel')

        # relabel some more columns to be consistent
        elec_df = elec_df.rename(columns={'channel': 'contact', 'tagName': 'label'})

    return elec_df


def load_eeg(events, rel_start_ms, rel_stop_ms, buf_ms=0, elec_scheme=None, noise_freq=[58., 62.],
             resample_freq=None, pass_band=None, use_mirror_buf=False, demean=False, do_average_ref=False):
    """
    Returns an EEG TimeSeries object.

    Parameters
    ----------
    events: pandas.DataFrame
        An events dataframe that contains eegoffset and eegfile fields
    rel_start_ms: int
        Initial time (in ms), relative to the onset of each event
    rel_stop_ms: int
        End time (in ms), relative to the onset of each event
    buf_ms:
        Amount of time (in ms) of buffer to add to both the begining and end of the time interval
    elec_scheme: pandas.DataFrame
        A dataframe of electrode information, returned by load_elec_info(). If the column 'contact' is in the dataframe,
        monopolar electrodes will be loads. If the columns 'contact_1' and 'contact_2' are in the df, bipolar will be
        loaded. You may pass in a subset of rows to only load data for electrodes in those rows.

        If you do not enter an elec_scheme, all monopolar channels will be loaded (but they will not be labeled with
        correct channel tags). Entering a scheme is recommended.
    noise_freq: list
        Stop filter will be applied to the given range. Default=(58. 62)
    resample_freq: float
        Sampling rate to resample to after loading eeg.
    pass_band: list
        If given, the eeg will be band pass filtered in the given range.
    use_mirror_buf: bool
        If True, the buffer will be data taken from within the rel_start_ms to rel_stop_ms interval,
        mirrored and prepended and appended to the timeseries. If False, data outside the rel_start_ms and rel_stop_ms
        interval will be read.
    demean: bool
        If True, will subject the mean voltage between rel_start_ms and rel_stop_ms from each channel
    do_average_ref: bool
        If True, will compute the average reference based on the mean voltage across channels

    Returns
    -------
    TimeSeries
        EEG timeseries object with dimensions channels x events x time (or bipolar_pairs x events x time)

        NOTE: The EEG data is returned with time buffer included. If you included a buffer and want to remove it,
              you may use the .remove_buffer() method. EXTRA NOTE: INPUT SECONDS FOR REMOVING BUFFER, NOT MS!!

    """

    # add buffer is using
    if (buf_ms is not None) and not use_mirror_buf:
        actual_start = rel_start_ms - buf_ms
        actual_stop = rel_stop_ms + buf_ms
    else:
        actual_start = rel_start_ms
        actual_stop = rel_stop_ms

    # load eeg
    # Should auto convert to PTSA? Any reason not to?
    eeg = CMLReader(subject=events.iloc[0].subject).load_eeg(events, rel_start=actual_start, rel_stop=actual_stop,
                                                        scheme=elec_scheme).to_ptsa()

    # compute average reference by subracting the mean across channels
    if do_average_ref:
        eeg = eeg - eeg.mean(dim='channel')

    # baseline correct subracting the mean within the baseline time range
    if demean:
        eeg = eeg.baseline_corrected([rel_start_ms, rel_stop_ms])

    # add mirror buffer if using. PTSA is expecting this to be in seconds.
    if use_mirror_buf:
        eeg = eeg.add_mirror_buffer(buf_ms / 1000.)

    # filter line noise
    if noise_freq is not None:
        if isinstance(noise_freq[0], float):
            noise_freq = [noise_freq]
        for this_noise_freq in noise_freq:
            b_filter = ButterworthFilter(eeg, this_noise_freq, filt_type='stop', order=4)
            eeg = b_filter.filter()

    # resample if desired. Note: can be a bit slow especially if have a lot of eeg data
    if resample_freq is not None:
        r_filter = ResampleFilter(eeg, resample_freq)
        eeg = r_filter.filter()

    # do band pass if desired.
    if pass_band is not None:
        eeg = band_pass_eeg(eeg, pass_band)

    # reorder dims to make events first
    eeg = make_events_first_dim(eeg)
    return eeg


def load_eeg_full_timeseries(task, subject, session,  elec_scheme=None, noise_freq=[58., 62.],
                             resample_freq=None, pass_band=None):
    """
    Function for loading continuous EEG data from a full session, not based on event times.
    Returns a list of timeseries object.

    task: str
        The experiment name
    subject: str
        The subject number
    session: int
        The session number for this subject and task
    elec_scheme: pandas.DataFrame
        A dataframe of electrode information, returned by load_elec_info(). If the column 'contact' is in the dataframe,
        monopolar electrodes will be loads. If the columns 'contact_1' and 'contact_2' are in the df, bipolar will be
        loaded. You may pass in a subset of rows to only load data for electrodes in those rows.

        If you do not enter an elec_scheme, all monopolar channels will be loaded (but they will not be labeled with
        correct channel tags). LOADING ALL ELECTRODES AND FOR AN ENTIRE SESSION AT ONCE IS NOT REALLY RECOMMENDED.
    noise_freq: list
        Stop filter will be applied to the given range. Default=(58. 62)
    resample_freq: float
        Sampling rate to resample to after loading eeg
    pass_band: list
        If given, the eeg will be band pass filtered in the given range

    Returns
    -------
    list
        A TimeSeries object.
    """

    # load eeg
    eeg = CMLReader(subject=subject, experiment=task, session=session).load_eeg(scheme=elec_scheme).to_ptsa()

    # filter line noise
    if noise_freq is not None:
        if isinstance(noise_freq[0], float):
            noise_freq = [noise_freq]
        for this_noise_freq in noise_freq:
            b_filter = ButterworthFilter(eeg, this_noise_freq, filt_type='stop', order=4)
            eeg = b_filter.filter()

    # resample if desired. Note: can be a bit slow especially if have a lot of eeg data
    if resample_freq is not None:
        r_filter = ResampleFilter(eeg, resample_freq)
        eeg = r_filter.filter()

    # do band pass if desired.
    if pass_band is not None:
        eeg = band_pass_eeg(eeg, pass_band)

    return eeg


def band_pass_eeg(eeg, freq_range, order=4):
    """
    Runs a butterworth band pass filter on an eeg time seriesX object.

    Parameters
    ----------
    eeg: timeseries
        A ptsa.timeseries object
    freq_range: list
        List of two floats defining the range to filter in
    order: int
        Order of butterworth filter

    Returns
    -------
    timeseries
        Filtered EEG object
    """
    return ButterworthFilter(eeg, freq_range, filt_type='pass', order=order).filter()


def compute_power(events, freqs, wave_num, rel_start_ms, rel_stop_ms, buf_ms=1000, elec_scheme=None,
                  noise_freq=[58., 62.], resample_freq=None, mean_over_time=True, log_power=True, loop_over_chans=True,
                  cluster_pool=None, use_mirror_buf=False, time_bins=None, do_average_ref=False):
    """
    Returns a TimeSeries object of power values with dimensions 'events' x 'frequency' x 'bipolar_pairs/channels' x
    'time', unless mean_over_time is True, then no 'time' dimenstion.

    Parameters
    ----------
    events: pandas.DataFrame
        An events structure that contains eegoffset and eegfile fields
    freqs: np.array or list
        A set of frequencies at which to compute power using morlet wavelet transform
    wave_num: int
        Width of the wavelet in cycles (I THINK)
    rel_start_ms: int
        Initial time (in ms), relative to the onset of each event
    rel_stop_ms: int
        End time (in ms), relative to the onset of each event
    buf_ms:
        Amount of time (in ms) of buffer to add to both the begining and end of the time interval before power
        computation. This buffer is automatically removed before returning the power timeseries.
    elec_scheme: pandas.DataFrame:
        A dataframe of electrode information, returned by load_elec_info(). If the column 'contact' is in the dataframe,
        monopolar electrodes will be loads. If the columns 'contact_1' and 'contact_2' are in the df, bipolar will be
        loaded. You may pass in a subset of rows to only load data for electrodes in those rows.

        If you do not enter an elec_scheme, all monopolar channels will be loaded (but they will not be labeled with
        correct channel tags). Entering a scheme is recommended.
    noise_freq: list
        Stop filter will be applied to the given range. Default=(58. 62)
    resample_freq: float
        Sampling rate to resample to after loading eeg but BEFORE computing power. So be careful. Don't downsample below
        your nyquist.
    mean_over_time: bool
        Whether to mean power over time, and return the power data with no time dimension
    log_power: bool
        Whether to log the power values
    loop_over_chans: bool
        Whether to process each channel independently, or whether to try to do all channels at once. Default is to loop
    cluster_pool: None or ipython cluster helper pool
        If given, will parallelize over channels
    use_mirror_buf: bool
        If True, a mirror buffer will be (used see load_eeg) instead of a normal buffer
    time_bins: list or array
        pairs of start and stop times in which to bin the data
    do_average_ref: bool
        If true, will load eeg and then compute an average reference before computing power. Note: This will load eeg
        for all channels at once, regardless of loop_over_chans or cluster_pool. Will still loop for power computation.
    Returns
    -------
    timeseries object of power values

    """

    # warn people if they set the resample_freq too low
    if (resample_freq is not None) and (resample_freq < (np.max(freqs)*2.)):
        print('Resampling EEG below nyquist frequency.')
        warnings.warn('Resampling EEG below nyquist frequency.')

    # make freqs a numpy array if it isn't already because PTSA is kind of stupid and can't handle a list of numbers
    if isinstance(freqs, list):
        freqs = np.array(freqs)

    # if doing an average reference, load eeg first
    if do_average_ref:
        eeg_all_chans = load_eeg(events, rel_start_ms, rel_stop_ms, buf_ms=buf_ms, elec_scheme=elec_scheme,
                                 noise_freq=noise_freq, resample_freq=resample_freq, use_mirror_buf=use_mirror_buf,
                                 do_average_ref=do_average_ref)
    else:
        eeg_all_chans = None

    # We will loop over channels if desired or if we are are using a pool to parallelize
    if cluster_pool or loop_over_chans:

        # must enter an elec scheme if we want to loop over channels
        if elec_scheme is None:
            print('elec_scheme must be entered if loop_over_chans is True or using a cluster pool.')
            return

        # put all the inputs into one list. This is so because it is easier to parallize this way. Parallel functions
        # accept one input. The pool iterates over this list.
        arg_list = [(events, freqs, wave_num, elec_scheme.iloc[r:r + 1], rel_start_ms, rel_stop_ms,
                     buf_ms, noise_freq, resample_freq, mean_over_time, log_power, use_mirror_buf, time_bins,
                     eeg_all_chans[:, r:r + 1] if eeg_all_chans is not None else None)
                    for r in range(elec_scheme.shape[0])]

        # if no pool, just use regular map
        if cluster_pool is not None:
            pow_list = cluster_pool.map(_parallel_compute_power, arg_list)
        else:
            pow_list = list(map(_parallel_compute_power, tqdm(arg_list, disable=True if len(arg_list) == 1 else False)))

        # This is the stupidest thing in the world. I should just be able to do concat(pow_list, dim='channels') or
        # concat(pow_list, dim='bipolar_pairs'), but for some reason it breaks. I don't know. So I'm creating a new
        # TimeSeries object

        # concatenate data
        chan_dim = pow_list[0].get_axis_num('channel')
        elecs = np.concatenate([x[x.dims[chan_dim]].data for x in pow_list])
        pow_cat = np.concatenate([x.data for x in pow_list], axis=chan_dim)

        # create new coordinates and Timeseries with concatenated data and electrode info
        new_coords = {x: (pow_list[0].coords[x] if x != 'channel' else elecs) for x in pow_list[0].coords.keys()}
        wave_pow = TimeSeries(data=pow_cat, coords=new_coords, dims=pow_list[0].dims)

    # if not looping, sending all the channels at once
    else:
        arg_list = [events, freqs, wave_num, elec_scheme, rel_start_ms, rel_stop_ms, buf_ms, noise_freq,
                    resample_freq, mean_over_time, log_power, use_mirror_buf, time_bins, eeg_all_chans]
        wave_pow = _parallel_compute_power(arg_list)

    # reorder dims to make events first
    wave_pow = make_events_first_dim(wave_pow)

    return wave_pow


def _parallel_compute_power(arg_list):
    """
    Returns a timeseries object of power values. Accepts the inputs of compute_power() as a single list. Probably
    don't really need to call this directly.
    """

    events, freqs, wave_num, elec_scheme, rel_start_ms, rel_stop_ms, buf_ms, noise_freq, resample_freq, mean_over_time, \
    log_power, use_mirror_buf, time_bins, eeg = arg_list

    # first load eeg
    if eeg is None:
        eeg = load_eeg(events, rel_start_ms, rel_stop_ms, buf_ms=buf_ms, elec_scheme=elec_scheme,
                       noise_freq=noise_freq, resample_freq=resample_freq, use_mirror_buf=use_mirror_buf)

    # then compute power
    wave_pow = MorletWaveletFilter(eeg, freqs, output='power', width=wave_num, cpus=12,
                                   verbose=False).filter()

    # remove the buffer
    wave_pow = wave_pow.remove_buffer(buf_ms / 1000.)

    # are we taking the log?
    if log_power:
        data = wave_pow.data
        wave_pow.data = numexpr.evaluate('log10(data)')

    # mean over time if desired
    if mean_over_time:
        wave_pow = wave_pow.mean(dim='time')

    # or take the mean of each time bin, if given
    # create a new timeseries for each bin and the concat and add in new time dimension
    elif time_bins is not None:
        ts_list = []
        time_list = []
        for t in time_bins:
            t_inds = (wave_pow.time >= t[0]) & (wave_pow.time <= t[1])
            ts_list.append(wave_pow.isel(time=t_inds).mean(dim='time'))
            time_list.append(wave_pow.time.data[t_inds].mean())
        wave_pow = xr.concat(ts_list, dim='time')
        wave_pow.coords['time'] = time_list

    return wave_pow


def make_events_first_dim(ts, event_dim_str='event'):
    """
    Transposes a TimeSeries object to have the events dimension first. Returns transposed object.

    Parameters
    ----------
    ts: TimeSeries
        A PTSA TimeSeries object
    event_dim_str: str
        the name of the event dimension

    Returns
    -------
    TimeSeries
        A transposed version of the orginal timeseries
    """

    # if events is already the first dim, do nothing
    if ts.dims[0] == event_dim_str:
        return ts

    # make sure events is the first dim because I think it is better that way
    ev_dim = np.where(np.array(ts.dims) == event_dim_str)[0]
    new_dim_order = np.hstack([ev_dim, np.setdiff1d(range(ts.ndim), ev_dim)])
    ts = ts.transpose(*np.array(ts.dims)[new_dim_order])
    return ts


def zscore_by_session(ts, event_dim_str='event'):
    """
    Returns a numpy array the same shape as the original timeseries, where all the elements have been zscored by
    session

    Returns
    -------
    numpy array
    """
    sessions = ts[event_dim_str].data['session']
    z_pow = np.empty(ts.shape)
    uniq_sessions = np.unique(sessions)
    for sess in uniq_sessions:
        sess_inds = sessions == sess
        z_pow[sess_inds] = zscore(ts[sess_inds], axis=0)
    return z_pow






