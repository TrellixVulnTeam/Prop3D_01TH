import os
from datetime import datetime

from molmimic.util import safe_remove
from molmimic.util.iostore import IOStore
from molmimic.util.cath import run_cath_hierarchy
from molmimic.util.hdf import get_file, filter_hdf_chunks
from molmimic.util.toil import map_job, map_job_follow_ons

from molmimic.generate_data.prepare_protein import process_domain
from molmimic.generate_data.calculate_features import calculate_features

from molmimic.generate_data import data_stores

from toil.realtimeLogger import RealtimeLogger

import logging
logging.getLogger('boto3').setLevel(logging.WARNING)
logging.getLogger('botocore').setLevel(logging.WARNING)
logging.getLogger('s3transfer').setLevel(logging.WARNING)
logging.getLogger('urllib3').setLevel(logging.WARNING)

def get_domain_structure_and_features(job, cath_domain, superfamily,
  cathFileStoreID, update_features=None, further_parallelize=False, force=False):
    """1) Run Feagtures depends on prepared strucutre Structure"""
    RealtimeLogger.info("get_domain_structure_and_features Process domain "+cath_domain)

    if force or not data_stores.prepared_cath_structures.exists(
      "{}/{}.pdb".format(superfamily, cath_domain)):
        #Get Processed domain file first
        if further_parallelize:
            job.addChildJobFn(process_domain, cath_domain, superfamily,
                cathFileStoreID=cathFileStoreID)
        else:
            try:
                process_domain(job, cath_domain, superfamily, cathFileStoreID=cathFileStoreID)
            except (SystemExit, KeyboardInterrupt):
                raise
            except:
                #Failed, do not proceed in calculating features
                return

    #Check if any feature files exist
    feat_files = ["{}/{}_{}".format(superfamily, cath_domain, ext) for ext in \
        ('atom.h5', 'residue.h5', 'edges.h5')]
    feats_exist = [data_stores.cath_features.exists(f) for f in feat_files]

    RealtimeLogger.info(f"get_domain_structure_and_features FEATURES {force} {update_features}, {all(feats_exist)}")

    if force or update_features is not None or not all(feats_exist):
        #Calculate features Processed domain file
        if further_parallelize:
            job.addFollowOnJobFn(calculate_features, cath_domain, superfamily,
                update_features=update_features)
        else:
            RealtimeLogger.info("get_domain_structure_and_features calculate_features")
            try:
                calculate_features(job, cath_domain, superfamily, update_features=update_features)
            except (SystemExit, KeyboardInterrupt):
                raise
            except:
                #Failed
                return

    if update_features is not None:
        jobStoreName = os.path.basename(job.fileStore.jobStore.config.jobStore.split(":")[-1])
        done_file = job.fileStore.getLocalTempFile()
        data_stores.data_eppic_cath_features.write_output_file(done_file,
            f"updates/{jobStoreName}/{superfamily}/{cath_domain}")
        safe_remove(done_file)

def process_superfamily(job, superfamily, cathFileStoreID, update_features=None,
  force=False, further_parallize=True):
    cath_file = job.fileStore.readGlobalFile(cathFileStoreID, cache=True)
    cathcode = superfamily.replace("/", ".")

    cath_domains = filter_hdf_chunks(
        cath_file,
        "table",
        columns=["cath_domain"],
        drop_duplicates=True,
        cathcode=cathcode)["cath_domain"].tolist()



    if not force and update_features is None:
        #Get domians that have edge features uploaded (last feature file to be uploaded so we know its done)
        done_domains = [os.path.basename(domain).split("_")[0] for domain in \
            data_stores.cath_features.list_input_directory(superfamily) \
            if domain.endswith("edges.txt.gz")]
        n_domains = len(cath_domains)
        cath_domains = list(set(cath_domains)-set(done_domains))
        RealtimeLogger.info("Running {}/{} domains from {}".format(len(cath_domains), n_domains, cathcode))
    elif update_features is not None:
        #oldest = datetime.date(2021, 2, 9)
        jobStoreName = os.path.basename(job.fileStore.jobStore.config.jobStore.split(":")[-1])
        updated_domains = [cath_domain for cath_domain, last_modified in \
            data_stores.data_eppic_cath_features.list_input_directory(
                f"updates/{jobStoreName}/{superfamily}/", with_times=True)] # \
                #if datetime.strptime(last_modified, '%Y-%m-%dT%H:%M:%S')<oldest]

        cath_domains = list(set(cath_domains)-set(updated_domains))
    else:
        RealtimeLogger.info("Running {} domains from {}".format(len(cath_domains), cathcode))

    if further_parallize:
        map_job(job, get_domain_structure_and_features, cath_domains,
            superfamily, cathFileStoreID, update_features=update_features,
            further_parallelize=False, force=force)
    else:
        RealtimeLogger.info("Looping over domain")
        for domain in cath_domains:
            try:
                RealtimeLogger.info("Processing domain "+domain)
                get_domain_structure_and_features(job, domain, superfamily,
                    cathFileStoreID, update_features=update_features,
                    further_parallelize=False, force=force)
            except (SystemExit, KeyboardInterrupt):
                raise
            except:
                pass

def start_toil(job, cathFileStoreID, cathcode=None, update_features=None, force=False):
    RealtimeLogger.info("started")

    cath_file = job.fileStore.readGlobalFile(cathFileStoreID, cache=True)

    cath_domains = filter_hdf_chunks(
        cath_file,
        "table",
        columns=["cath_domain", "cathcode"],
        drop_duplicates=True)

    if cathcode is not None:
        if not isinstance(cathcode, (list, tuple)):
            cathcode = [cathcode]
        RealtimeLogger.info("Running sfams: {}".format(cathcode))

        if update_features is None and not force:
            done_domains = [os.path.basename(domain).split("_")[0] for sfam in cathcode \
                for domain in data_stores.cath_features.list_input_directory(sfam.replace(".", "/")) \
                if domain.endswith("edges.txt.gz")]
        else:
            done_domains = None
    else:
        if update_features is None and not force:
            done_domains = [os.path.basename(domain).split("_")[0] for domain in \
                data_stores.cath_features.list_input_directory() \
                if domain.endswith("edges.txt.gz")]
        else:
            done_domains = None

    if update_features is None:
        domains_to_run = cath_domains[~cath_domains["cath_domain"].isin(done_domains)]
    else:
        domains_to_run = cath_domains

    RealtimeLogger.info("Domains to from {} superfamilies to run: {}".format(
        len(cathcode) if cathcode is not None else "all", len(domains_to_run)))

    if len(domains_to_run) > 500:
        #Start CATH hiearchy
        run_cath_hierarchy(job, cathcode, process_superfamily, cathFileStoreID,
            update_features=update_features, force=force)
    else:
        superfamilies = domains_to_run["cathcode"].drop_duplicates().str.replace(".", "/")
        RealtimeLogger.info("Superfamilies to run: {}".format(len(superfamilies)))
        map_job(job, process_superfamily, superfamilies, cathFileStoreID,
            update_features=update_features, force=force)

    #Build Interactome
    #job.addChildJobFn()

if __name__ == "__main__":
    from toil.common import Toil
    from toil.job import Job

    parser = Job.Runner.getDefaultArgumentParser()
    parser.add_argument(
        "-c", "--cathcode",
        nargs='+',
        default=None)
    parser.add_argument(
        "--features",
        nargs="+",
        default=None
    )
    parser.add_argument(
        "--force",
        action="store_true",
        default=False)
    options = parser.parse_args()
    options.logLevel = "DEBUG"
    #options.clean = "always"
    options.targetTime = 1

    if options.cathcode is not None:
        options.cathcode = [c.split(".") for c in options.cathcode]

    sfam_file = os.path.abspath("cath.h5")
    if not os.path.isfile(sfam_file):
        store = IOStore.get("aws:us-east-1:molmimic-cath")
        store.read_input_file("cath-domain-description-file-small.h5", sfam_file)

    with Toil(options) as workflow:
        if not workflow.options.restart:
            cathFileStoreID = workflow.importFile("file://" + os.path.abspath(sfam_file))
            job = Job.wrapJobFn(start_toil, cathFileStoreID, cathcode=options.cathcode,
                update_features=options.features, force=options.force)
            workflow.start(job)
        else:
            workflow.restart()
