import awkward as ak
import numpy as np
import tensorflow as tf
from collections import namedtuple

from glob import glob
from itertools import zip_longest, islice
from collections import deque
import multiprocessing as mp

default_features_dict = {
        "cl_features" : [ "en_cluster","et_cluster",
                        "cluster_eta", "cluster_phi", 
                        "cluster_ieta","cluster_iphi","cluster_iz",
                        "cluster_deta", "cluster_dphi",
                        "cluster_den_seed","cluster_det_seed",
                        "en_cluster_calib", "et_cluster_calib",
                        "cl_f5_r9", "cl_f5_sigmaIetaIeta", "cl_f5_sigmaIetaIphi",
                        "cl_f5_sigmaIphiIphi","cl_f5_swissCross",
                        "cl_r9", "cl_sigmaIetaIeta", "cl_sigmaIetaIphi",
                        "cl_sigmaIphiIphi","cl_swissCross",
                        "cl_nxtals", "cl_etaWidth","cl_phiWidth"],


    "cl_metadata": [ "calo_score", "calo_simen_sig", "calo_simen_PU",
                     "cluster_PUfrac","calo_nxtals_PU",
                     "noise_en","noise_en_uncal","noise_en_nofrac","noise_en_uncal_nofrac" ],

    "cl_labels" : ["is_seed","is_calo_matched","is_calo_seed", "in_scluster","in_geom_mustache","in_mustache"],

    
    "seed_features" : ["seed_eta","seed_phi", "seed_ieta","seed_iphi", "seed_iz", 
                     "en_seed", "et_seed","en_seed_calib","et_seed_calib",
                    "seed_f5_r9","seed_f5_sigmaIetaIeta", "seed_f5_sigmaIetaIphi",
                    "seed_f5_sigmaIphiIphi","seed_f5_swissCross",
                    "seed_r9","seed_sigmaIetaIeta", "seed_sigmaIetaIphi",
                    "seed_sigmaIphiIphi","seed_swissCross",
                    "seed_nxtals","seed_etaWidth","seed_phiWidth"
                    ],

    "seed_metadata": [ "seed_score", "seed_simen_sig", "seed_simen_PU", "seed_PUfrac"],
    "seed_labels" : [ "is_seed_calo_matched", "is_seed_calo_seed", "is_seed_mustache_matched"],

     "window_features" : [ "max_en_cluster","max_et_cluster","max_deta_cluster","max_dphi_cluster","max_den_cluster","max_det_cluster",
                         "min_en_cluster","min_et_cluster","min_deta_cluster","min_dphi_cluster","min_den_cluster","min_det_cluster",
                         "mean_en_cluster","mean_et_cluster","mean_deta_cluster","mean_dphi_cluster","mean_den_cluster","mean_det_cluster" ],

    "window_metadata": ["flavour", "ncls", "nclusters_insc",
                        "nVtx", "rho", "obsPU", "truePU",
                        "sim_true_eta", "sim_true_phi",  
                        "gen_true_eta","gen_true_phi",
                        "en_true_sim","et_true_sim", "en_true_gen", "et_true_gen",
                        "en_true_sim_good", "et_true_sim_good",
                        "en_mustache_raw", "et_mustache_raw","en_mustache_calib", "et_mustache_calib",
                        "max_en_cluster_insc","max_deta_cluster_insc","max_dphi_cluster_insc",
                        "event_tot_simen_PU","wtot_simen_PU","wtot_simen_sig" ],
}


##### Configuration namedtuple

from typing import List
from dataclasses import dataclass, field


@dataclass
class LoaderConfig():
    # list of input files [[fileA1, fileB1], [fileA2, fileB2]]
    # each list will be handled by worker in parallel.
    # Different files in each list will be interleaved and shuffled for each chunk.
    input_files : List[List[str]]  = field(default_factory=list)
    # in alternative a list of directories to be zipped together can be provided
    # The files from each folder will be zipped and samples shuffled together.    
    input_folders : List[str] = field(default_factory=list )
    # Group of records to read from awk files
    file_input_columns: List[str] = field(default_factory=lambda : ["cl_features", "cl_labels",
                                                                "window_features", "window_metadata", "cl_h"])
    # specific fields to read out for each cl, window, labels..
    columns: dict[str] = field(default_factory=lambda: default_features_dict) 
    padding: bool = True, # zero padding or not
    # if -1 it will be dynami# c for each batch,
    #if >0 it will be a fix number with clippingq
    ncls_padding: int = 45 
    nhits_padding: int = 45 # as ncls_padding
    # dimension of the chunk to read at once from each file,
    # must be a multiple of the batch_size                         
    chunk_size: int = 256*20
    batch_size: int = 256 # final batch size of the arrays
    maxevents: int = 2560  # maximum number of event to be read in total
    offset: int = 0     # Offset for reading records from each file
    # normalization strategy for cl_features and window_features,
    # stdscale, minmax,or None
    norm_type: str = "stdscale"
    norm_factors_file: str = "normalization_factors_v1.json"     #file with normalization factors
    norm_factors: dict = None     #normalization factors array dictionary
    nworkers: int = 2,   # number of parallele process to use to read files
    max_batches_in_memory: int = 30 #  number of batches to load at max in memory



########################################################
### Utility functions to build the generator chain   ###
########################################################

def load_dataset_chunks(df, config, chunk_size, offset=0, maxevents=None):
    # Filtering the columns to keey only the requested ones
    cols = { key: df[key][v] for key, v in config.columns.items() }
    # Adding the clusters hits 
    cols['cl_h'] = df.cl_h
    filtered_df = ak.zip(cols, depth_limit=1)
    # Now load in large chunks batching
    if maxevents:
        nchunks = maxevents // chunk_size
    else:
        nchunks = ak.num(filtered_df.cl_features, axis=0)//chunk_size 
    for i in range(nchunks):
        # Then materialize it
        yield chunk_size, ak.materialized(filtered_df[offset + i*chunk_size: offset + (i+1)*chunk_size])
        #yield batch_size, df[i*batch_size: (i+1)*batch_size]
        
def split_batches(gen, batch_size):
    for size, df in gen:
        if size % batch_size == 0:
            for i in range(size//batch_size):
                if isinstance(df, tuple):
                    yield batch_size, tuple(d[i*batch_size : (i+1)*batch_size] for d in df)
                else:
                    yield batch_size, df[i*batch_size : (i+1)*batch_size]
        else:
            raise Exception("Please specifie a batchsize compatible with the loaded chunks size")
        
def buffer(gen,size):
    ''' This generator buffer a number `size` of elements from an iterator and yields them. 
    When the buffer is empty the quee is filled again'''
    q = deque()
    while True:
        # Caching in the the queue some number of elements
        in_q = 0
        try:
            for _ in range(size):
                q.append(next(gen))
                in_q +=1
        except StopIteration:
            for _ in range(in_q):
                yield q.popleft()
            break
        # Now serve them
        for _ in range(in_q):
            yield q.popleft()
        
def shuffle_fn(size, df):
    try:
        perm_i = np.random.permutation(size)
        return size, df[perm_i]
    except:
        return 0, ak.Array([])
    
    
def shuffle_dataset(gen, n_batches=None):
    if n_batches==None: 
        # permute the single batch
        for i, (size, df) in enumerate(gen):
            yield shuffle_fn(size, df)
    else:
        for dflist in cache_generator(gen, n_batches):
            size = dflist[0][0] 
            perm_i = np.random.permutation(size*len(dflist))
            totdf = ak.concatenate([df[1] for df in dflist])[perm_i]
            for i in range(n_batches):
                yield size, totdf[i*size: (i+1)*size]
                
def zip_datasets(*iterables):
    yield from zip_longest(*iterables, fillvalue=(0, ak.Array([])))
    
def concat_fn(dfs):
    return sum([d[0] for d in dfs]), ak.concatenate([d[1] for d in dfs])

def concat_datasets(*iterables):
    for dfs in zip_datasets(*iterables):
        yield concat_fn(dfs)
        
def to_flat_numpy(X, axis=2, allow_missing=True):
    return np.stack([ak.to_numpy(X[f], allow_missing=allow_missing) for f in X.fields], axis=axis)

def convert_to_tf(df):
    return [ tf.convert_to_tensor(d) for d in df ]

##############################################################################################
# Multiprocessor generator running a separate process for each group of
# input files. The result of each process is put in a queue and consumed by the main thread.

def multiprocessor_generator_from_files(files, internal_generator, output_queue_size=40, nworkers=4, maxevents=None):
    '''
    Generator with multiprocessing working on a list of input files.
    All the input files are put in a Queue that is consumed by a Pool of workers. 
    Each worker passes the file to the `internal_generator` and consumes it. 
    The output is put in an output Queue which is consumed by the main thread.
    Doing so the processing is in parallel. 
    '''
    def process(input_q, output_q):
        # Change the random seed for each processor
        pid = mp.current_process().pid
        np.random.seed()
        while True:
            file = input_q.get()
            if file is None:
                output_q.put(None)
                break
            # We give the file to the generator and then yield from it
            for out in internal_generator(file):
                output_q.put(out)
    
    input_q = mp.Queue()
    # Load all the files in the input file
    for file in files: 
        input_q.put(file)
    # Once generator is consumed, send end-signal
    for i in range(nworkers):
        input_q.put(None)
    
    #output_q = mp.Queue(maxsize=output_queue_size)
    output_q = mp.SimpleQueue()
    # Here we need 2 groups of worker :
    # * One that do the main processing. It will be `pool`.
    # * One that read the results and yield it back, to keep it as a generator. The main thread will do it.
    pool = mp.Pool(nworkers, initializer=process, initargs=(input_q, output_q))
    
    try : 
        finished_workers = 0
        tot_events = 0
        while True:
            it = output_q.get()
            if it is None:
                finished_workers += 1
                if finished_workers == nworkers:
                    break
            else:
                size, df = it
                tot_events += size
                if maxevents and tot_events > maxevents:
                    break
                else:
                    yield it
    finally: 
        # This is called at GeneratorExit
        pool.close()
        pool.terminate()
        #print("Multiprocessing generator closed")
            

###############################################################

 

def load_batches_from_files_generator(config, preprocessing_fn):
    '''
    Generator reading full batches from a list of files.
    The process is the following:
    - a chunk is read from each file in the list
    - chunks get concatenated
    - samples are shuffled
    - a preprocessing function is applied on the shuffled samples
    - the chunk is split in batches and returned as a generator.
    
    A config file is needed to specify which columns are read from the files,
    padding, and the size of chunks and batched.

    N.B.: the chunk size must be a multiple of the batch size. 
    '''
    def _fn(files): 
        # Parquet files
        dfs_raw = [ ak.from_parquet(file, lazy=True, use_threads=True, columns=config.file_input_columns) for file in files if file!=None]
        # Loading chunks from the files
        initial_dfs = [ load_dataset_chunks(df, config, chunk_size=config.chunk_size, offset=config.offset) for df in dfs_raw] 
        # Contatenate the chunks from the list of files
        concat_df = concat_datasets(*initial_dfs)
        # Shuffle the axis=0
        shuffled = shuffle_dataset(concat_df)
        # Processing the data to extract X,Y, etc
        _preprocess_fn = preprocessing_fn(config)
        processed  = (_preprocess_fn(d) for d in shuffled)
        # Split in batches
        #yield from processed
        yield from split_batches(processed, config.batch_size)
    
    return _fn


###########################################################################################################
# Preprocessing function to prepare numpy data for training
def preprocessing(config):
    '''
    Preprocessing function preparing the data to be in the format needed for training.
     Several zero-padded numpy arrays are retured:
     - Cluster features (batchsize, Nclusters, Nfeatures)
     - Cluster labels (batchsize, Nclusters, Nlabels)
     - is_seed mask (batchsize, Ncluster)
     - Rechits (batchsize, Nclusters, Nrechits, Nrechits_features)
     - Window features (batchsize, Nwind_features)
     - Window metadata (batchsize, Nwind_meatadata)
     - flavour (ele/gamma/jets) (batchsize,)
     - Rechits padding mask  (batchsize, Ncluster, Nrechits, 1)
     - Clusters padding mask (batchsize, Ncluster, 1)

    The config for the function contains all the info and have the format
     The zero-padding can be fixed side (specified in the config dizionary),
     or computed dinamically for each chunk.
     
    '''
    def process_fn(data): 
        size, df = data
        # Extraction of the ntuples and zero padding

        #padding
        if config.padding:
            if config.ncls_padding == -1:
                # dynamic padding
                max_ncls = ak.max(ak.num(df.cl_features, axis=1))
            else:
                max_ncls = config.ncls_padding
            if config.nhits_padding == -1:
                max_nhits = ak.max(ak.num(df.cl_h, axis=2))
            else:
                max_nhits = config.nhits_padding

            cls_X_pad = ak.pad_none(df.cl_features, max_ncls, clip=True)
            cls_Y_pad = ak.pad_none(df.cl_labels, max_ncls, clip=True)
            wind_X = df.window_features
            wind_meta = df.window_metadata
            is_seed_pad = ak.pad_none(df.cl_labels["is_seed"], max_ncls, clip=True)

            # cls_X_pad = ak.fill_none(cls_X_pad, {k:0 for k in config.columns["cl_features"]})
            # cls_Y_pad = ak.fill_none(cls_Y_pad, 0.)
            # is_seed_pad = ak.fill_none(is_seed_pad, False)
            # hits padding
            cl_hits_padrec = ak.pad_none(df.cl_h, max_nhits, axis=2, clip=True) # --> pad rechits dim
            cl_hits_padded = ak.pad_none(cl_hits_padrec, max_ncls, axis=1, clip=True) # --> pad ncls dimension
            # h_padh_padcl_fillnoneCL = ak.fill_none(h_padh_padcl, [None]*max_nhits, axis=1) #-- > fill the out dimension with None
            # cl_hits_pad = np.asarray(ak.fill_none(h_padh_padcl_fillnoneCL, [0.,0.,0.,0.] , axis=2)) # --> fill the padded rechit dim with 0.
           
            cls_X_pad_n = to_flat_numpy(cls_X_pad, axis=2, allow_missing=True)
            cls_Y_pad_n = to_flat_numpy(cls_Y_pad, axis=2, allow_missing=True)
            is_seed_pad_n = ak.to_numpy(is_seed_pad, allow_missing=True)
            cl_hits_pad_n = ak.to_numpy(cl_hits_padded, allow_missing=True)
            wind_X_n = to_flat_numpy(wind_X, axis=1)
            wind_meta_n = to_flat_numpy(wind_meta, axis=1)
            
            # Masks for padding
            hits_mask = np.array(np.any(~cl_hits_pad_n.mask, axis=-1), dtype=int)
            cls_mask = np.array(np.any(hits_mask, axis=-1), dtype=int)
            #adding the last dim for broadcasting the 0s
            hits_mask = hits_mask[:,:,:,None]
            cls_mask = cls_mask[:,:,None]
            
            # Normalization
            norm_fact = config.norm_factors
            if config.norm_type == "stdscale":
                # With remasking
                cls_X_pad_n = ((cls_X_pad_n - norm_fact["cluster"]["mean"])/ norm_fact["cluster"]["std"] ) * cls_mask
                wind_X_n =  ((wind_X_n - norm_fact["window"]["mean"])/ norm_fact["window"]["std"] )  
            elif config.norm_type == "minmax":
                cls_X_pad_n = ((cls_X_pad_n - norm_fact["cluster"]["min"])/ (norm_fact["cluster"]["max"]-norm_fact["cluster"]["min"])) * cls_mask
                wind_X_n =  ((wind_X_n - norm_fact["window"]["min"])/ (norm_fact["window"]["max"]-norm_fact["window"]["min"]) )  
            
            flavour = np.asarray(df.window_metadata.flavour)
            
            return size, ( cls_X_pad_n, cls_Y_pad_n, is_seed_pad_n, cl_hits_pad_n,
                           wind_X_n, wind_meta_n, flavour, hits_mask, cls_mask)
        else:
            cls_X = df.cl_features, max_ncls
            cls_Y = df.cl_labels["in_scluster"], max_ncls
            is_seed = df.cl_labels["is_seed"], max_ncls
            cl_hits = df.cl_h
            return size, (cls_X, cls_Y, is_seed, cl_hits, flavour)
            
    return process_fn


###########################################################################################################
# Function reading from file the normalization factors 

def get_norm_factors(norm_file, cl_features, wind_features, numpy=True):
    # Loading the factors from file
    norm_factors = ak.from_json(norm_file)
    if numpy:
        return {
            "cluster" : {
                "mean": to_flat_numpy(norm_factors["cluster"]["mean"][cl_features], axis=0),
                "std": to_flat_numpy(norm_factors["cluster"]["std"][cl_features], axis=0),
                "min": to_flat_numpy(norm_factors["cluster"]["min"][cl_features], axis=0),
                "max": to_flat_numpy(norm_factors["cluster"]["max"][cl_features], axis=0)
            },
            "window":{
                "mean": to_flat_numpy(norm_factors["window"]["mean"][wind_features], axis=0),
                "std": to_flat_numpy(norm_factors["window"]["std"][wind_features], axis=0),
                "min": to_flat_numpy(norm_factors["window"]["min"][wind_features], axis=0),
                "max": to_flat_numpy(norm_factors["window"]["max"][wind_features], axis=0)
            }
        }
    else:
        return {
            "cluster" : {
                "mean": norm_factors["cluster"]["mean"][cl_features],
                "std": norm_factors["cluster"]["std"][cl_features],
                "min": norm_factors["cluster"]["min"][cl_features],
                "max": norm_factors["cluster"]["max"][cl_features],
            },
            "window":{
                "mean": norm_factors["window"]["mean"][wind_features],
                "std": norm_factors["window"]["std"][wind_features], 
                "min": norm_factors["window"]["min"][wind_features], 
                "max": norm_factors["window"]["max"][wind_features], 
            }
        }


#####################################################################################################
### Tensorflow tensors conversion


def tf_generator(config):
    def _gen():
        file_loader_generator = load_batches_from_files_generator(config, preprocessing)
        multidataset = multiprocessor_generator_from_files(config.input_files, 
                                                           file_loader_generator, 
                                                           output_queue_size=config.max_batches_in_memory, 
                                                           nworkers=config.nworkers, 
                                                           maxevents=config.maxevents)
       
        for size, df in multidataset:
            tfs = convert_to_tf(df)
            yield tuple(tfs)
    return _gen


################################
# User API to get a dataset general

def load_dataset (config: LoaderConfig):
    '''
    Function exposing to the end user the tensorflow dataset loading through the awkward chain. 
    '''
    # Check if folders instead of files have been provided
    if config.input_folders:
        config.input_files = list(zip_longest([glob(folder+"/*.parquet") for folder in config.input_folders]))
    if not config.input_folders and not config.input_files:
        raise Exception("No input folders or files provided! Please provide some input!")
    # Load the normalization factors
    if config.norm_factors == None and config.norm_factors_file:
        config.norm_factors = get_norm_factors(config.norm_factors_file, config.columns["cl_features"], config.columns["window_features"])
    #cls_X_pad_n, cls_Y_pad_n, is_seed_pad_n, cl_hits_pad_n,  wind_X_n, wind_meta_n, flavour, hits_mask, cls_mask
    df = tf.data.Dataset.from_generator(tf_generator(config), 
       output_signature= (
         tf.TensorSpec(shape=(None,None,len(config.columns["cl_features"])), dtype=tf.float64), # cl_x (batch, ncls, #cl_x_features)
         tf.TensorSpec(shape=(None,None, len(config.columns["cl_labels"])), dtype=tf.bool),  #cl_y (batch, ncls, #cl_labels)
         tf.TensorSpec(shape=(None,None), dtype=tf.bool),  # is seed (batch, ncls,)
         tf.TensorSpec(shape=(None,None, None, 4), dtype=tf.float64), #hits  (batch, ncls, nhits, 4)
         tf.TensorSpec(shape=(None,len(config.columns["window_features"])), dtype=tf.float64),  #windox_X (batch, #wind_x)
         tf.TensorSpec(shape=(None,len(config.columns["window_metadata"])), dtype=tf.float64),  #windox_metadata (batch, #wind_meta)
         tf.TensorSpec(shape=(None,), dtype=tf.int32),  # flavour (batch,)
         tf.TensorSpec(shape=(None,None,None,1), dtype=tf.int32), #hits mask
         tf.TensorSpec(shape=(None,None,1), dtype=tf.int32),   #clusters mask
     ))
 
    return df
