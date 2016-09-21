import argparse
import glob
import os

import pandas as pd
import numpy as np
from scipy import special
import pyfits

from src import C_Funct
import auto_waterfaller

def main():
  def parser():
    # Command-line options
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter,
                                    description="The program groupes events from single_pulse_search.py in pulses.")
    parser.add_argument('-fits', help="Filename of .fits file.", default='*.fits')
    parser.add_argument('-idL', help="Basename of .singlepulse files.", default='*')
    parser.add_argument('-folder', help="Path of the folder containig the .singlepulse files.", default='.')
    parser.add_argument('-store_events', help="Store events in a SinglePulses.hdf5 database.", action='store_true')
    parser.add_argument('-events_dt', help="Duration in sec within two events are related to the same pulse.", default=20e-3,
                        type=int)
    parser.add_argument('-events_dDM', help="Number of DM steps within two events are related to the same pulse.", 
                        default=5, type=float)
    parser.add_argument('-DM_step', help="Value of the DM step between timeseries.", 
                        default=1., type=float)
    parser.add_argument('-events_database', help="Load events from this database.")
    parser.add_argument('-pulses_database', help="Load pulses from this database.")
    parser.add_argument('-store_dir', help="Path of the folder to store the output database.", default='.')
    parser.add_argument('-plot_pulses', help="Save plots of the detected pulses.", action='store_true')
    return parser.parse_args()
  args = parser()
  
  header = fits_header(args.fits)
  
  if isinstance(args.pulses_database, str): pulses = pd.read_hdf(args.pulses_database,'pulses')
  else: 
    pulses = pulses_database(args, header)
    store = pd.HDFStore('{}/SinglePulses.hdf5'.format(args.store_dir), 'a')
    store.append('pulses',pulses)
    store.close()
  
  if args.plot_pulses: 
    fits_name = glob.glob(args.fits)[0]
    fits_name = os.path.basename(fits_name)
    auto_waterfaller.main(args.fits, np.array(pulses.Time), np.array(pulses.DM), directory=fits_name)
  
  return


def events_database(args):
  #Create events database
  sp_files = glob.glob("{}/{}*.singlepulse".format(args.folder, args.idL))
  events = pd.concat(pd.read_csv(f, delim_whitespace=True, dtype=np.float32) for f in sp_files)
  events.reset_index(drop=True, inplace=True)
  events.columns = ['a','DM','Sigma','Time','Sample','Downfact','b']
  events = events.ix[:,['DM','Sigma','Time','Sample','Downfact']]
  events.index.name = 'idx'
  events['Pulse'] = 0
  events.Pulse = events.Pulse.astype(np.int32)
  events.sort(['DM','Time'],inplace=True)
  C_Funct.Get_Group(events.DM.values, events.Sigma.values, events.Time.values, events.Pulse.values, 
                    args.events_dDM, args.events_dt, args.DM_step)
  if args.store_events:
    store = pd.HDFStore('{}/SinglePulses.hdf5'.format(args.store_dir), 'w')
    store.append('events',events,data_columns=['Pulse','SAP','BEAM','DM','Time'])
    store.close()
    
  return events[events.Pulse >= 0]
  
  
def pulses_database(args, header, events=None):
  #Create pulses database
  if isinstance(args.events_database, str): events = pd.read_hdf(args.events_database,'events')
  elif not isinstance(events, pd.DataFrame): events = events_database(args)
  gb = events.groupby('Pulse',sort=False)
  pulses = events.loc[gb.Sigma.idxmax()]
  pulses.index = pulses.Pulse
  pulses.index.name = None
  pulses = pulses.loc[:,['DM','Sigma','Time','Sample','Downfact']]
  pulses.index.name = 'idx'
  pulses['MJD'] = header['STT_IMJD'] + (header['STT_SMJD'] + pulses.Time) / 86400.
  pulses['Duration'] = pulses.Downfact * header['TBIN']
  pulses['top_Freq'] = header['OBSFREQ'] + abs(header['OBSBW']) / 2.
  pulses['Pulse'] = 0
  pulses.Pulse = pulses.Pulse.astype(np.int8)
  pulses['dDM'] = (gb.DM.max() - gb.DM.min()) / 2.
  pulses.dDM=pulses.dDM.astype(np.float32)
  pulses['dTime'] = (gb.Time.max() - gb.Time.min()) / 2.
  pulses.dTime=pulses.dTime.astype(np.float32)
  pulses['N_events'] = gb.DM.count()
  pulses.N_events = pulses.N_events.astype(np.int16)

  pulses = pulses[pulses.N_events > 5]
  pulses = pulses[pulses.Sigma >= 8.]
  pulses = pulses[pulses.Downfact <= 100]
  
  RFIexcision(events, pulses)
  
  pulses.sort('Sigma', ascending=False, inplace=True)
  return pulses[pulses.Pulse == 0]
  

def RFIexcision(events, pulses):
  events = events[events.Pulse.isin(pulses.index)]
  events.DM = events.DM.astype(np.float64)
  events.Sigma = events.Sigma.astype(np.float64)
  events.sort('DM',inplace=True)
  gb = events.groupby('Pulse')
  pulses.sort_index(inplace=True)
  
  #Remove flat SNR pulses. Minimum ratio to have weakest pulses with SNR = 8
  pulses.Pulse[pulses.Sigma / gb.Sigma.min() <= 8./6.] += 1
  
  #Remove flat duration pulses. Minimum ratio to have weakest pulses with SNR = 8 (from Eq.6.21 of Pulsar Handbook)
  pulses.Pulse[gb.Downfact.max() / pulses.Downfact < 1.8] += 1
  
  #Remove pulses peaking near the DM edges
  DM_low = 461.
  DM_high = 660.
  DM_frac = (DM_high - DM_low) * 0.2  #Remove 5% of DM range from each edge
  pulses.Pulse[(pulses.DM < DM_low+DM_frac) | (pulses.DM > DM_high-DM_frac)] += 1
  
  #Remove pulses intersecting half the maximum SNR different than 2 or 4 times
  def crosses(sig):
    diff = sig - (sig.max() + sig.min()) / 2.
    count = np.count_nonzero(np.diff(np.sign(diff)))
    return (count != 2) & (count != 4)
  pulses.Pulse += gb.apply(lambda x: crosses(x.Sigma)).astype(np.int8)
    
  return


def fits_header(filename):
  with pyfits.open(filename,memmap=True) as fits:
    header = fits['SUBINT'].header + fits['PRIMARY'].header
  return header
  

if __name__ == '__main__':
  main()
