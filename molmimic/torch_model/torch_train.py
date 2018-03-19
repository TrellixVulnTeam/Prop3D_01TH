import sys
sys.path.append("/data/draizene/molmimic")
#sys.path.append("/usr/share/pdb2pqr")
#sys.path.append("/data/draizene/seaborn")
#sys.path.append("/data/draizene/pytorchviz")
#sys.path.append("/data/draizene/torchbiomed")

import os
import time
import math
import multiprocessing
from datetime import datetime
from itertools import izip

import numpy as np
import gc
import torch
from torch.optim import Adam, SGD
from torch.autograd import Variable
from torch.optim.lr_scheduler import StepLR, LambdaLR
from torchnet.logger import MeterLogger

import sparseconvnet as scn

from tqdm import tqdm

from molmimic.torch_model.Loss import DiceLoss
import molmimic.torch_model.Loss as LossFunctions
from molmimic.torch_model.torch_model import UNet3D, ResNetUNet
from molmimic.torch_model.torch_loader import IBISDataset, IBISUnclusteredDataset
from molmimic.torch_model.ModelStats import ModelStats, add_to_logger, format_meter, graph_logger

from torchviz import dot
#import torchbiomed.loss as bioloss

import subprocess
subprocess.call("python -c 'import visdom.server as vs; vs.main()' &", shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

def train(ibis_data, input_shape=(264,264,264), model_prefix=None, check_point=True, save_final=True, only_aa=False, only_atom=False, non_geom_features=False, use_deepsite_features=False, expand_atom=False, num_workers=None, num_epochs=30, batch_size=20, shuffle=True, use_gpu=True, initial_learning_rate=0.0001, learning_rate_drop=0.5, learning_rate_epochs=10, lr_decay=4e-2, data_split=0.8, course_grained=False, no_batch_norm=False, use_resnet_unet=False, unclustered=False, undersample=False, oversample=False, nFeatures=3, allow_feature_combos=False, bs_feature=None, bs_feature2=None, bs_features=None, stripes=False, data_parallel=False, dropout_depth=False, dropout_width=False, dropout_p=0.5):
    if model_prefix is None:
        model_prefix = "./molmimic_model_{}".format(datetime.now().strftime('%Y-%m-%d_%H:%M:%S'))

    if num_workers is None:
        num_workers = multiprocessing.cpu_count()-1

    since = time.time()

    if ibis_data == "spheres":
        from torch_loader import SphereDataset
        datasets = SphereDataset.get_training_and_validation(input_shape, cnt=1, n_samples=1000, nFeatures=nFeatures, allow_feature_combos=allow_feature_combos, bs_feature=bs_feature, bs_feature2=bs_feature2, bs_features=bs_features, stripes=stripes, data_split=0.99)
        validation_batch_size = 1
        if bs_features is not None:
            nClasses = len(bs_features)+1
        else:
            nClasses = 2
    elif os.path.isfile(ibis_data):
        dataset = IBISDataset if not unclustered else IBISUnclusteredDataset

        if allow_feature_combos and nFeatures is not None:
            random_features = (nFeatures, allow_feature_combos, bs_feature, bs_feature2)
        elif not allow_feature_combos and nFeatures is not None:
            random_features = (nFeatures, False, bs_feature, bs_feature2)
        elif allow_feature_combos is not None and nFeatures is None:
            random_features = None
            print "ignoring --allow-feature-combos"
        else:
            random_features = None

        datasets = dataset.get_training_and_validation(
            ibis_data,
            input_shape=input_shape,
            only_aa=only_aa,
            only_atom=only_atom,
            non_geom_features=non_geom_features,
            use_deepsite_features=use_deepsite_features,
            data_split=data_split,
            course_grained=course_grained,
            oversample=oversample,
            undersample=undersample,
            random_features=random_features)
        nFeatures = datasets["train"].get_number_of_features()
        nClasses = 2

        validation_batch_size = batch_size
    else:
        raise RuntimeError("Invalid training data")

    if num_workers%2 == 0:
        num_workers -= 1
    num_workers /= 2
    num_workers = 6

    dataloaders = {name:dataset.get_data_loader(
        batch_size if dataset.train else validation_batch_size,
        shuffle,
        num_workers) \
        for name, dataset in datasets.iteritems()}

    dtype = 'torch.cuda.FloatTensor' if torch.cuda.is_available() else 'torch.FloatTensor'

    if use_resnet_unet:
        model = ResNetUNet(nFeatures, nClasses, dropout_depth=dropout_depth, dropout_width=dropout_width, dropout_p=dropout_p)
        #criterion = torchbiomed.loss.DiceLoss #torch.nn.NLLLoss
    else:
        model = UNet3D(nFeatures, nClasses, batchnorm=not no_batch_norm)
        #criterion = torchbiomed.loss.DiceLoss

    if data_parallel:
        model = torch.nn.DataParallel(model)

    model.type(dtype)

    optimizer = SGD(model.parameters(),
        lr = initial_learning_rate,
        momentum = 0.999,
        weight_decay=1e-4,
        nesterov=True)

    if False:
        scheduler = StepLR(optimizer, step_size=1, gamma=learning_rate_drop)
    else:
        scheduler = LambdaLR(optimizer, lambda epoch: math.exp((1 - epoch) * lr_decay))

    check_point_model_file = "{}_checkpoint_model.pth".format(model_prefix)
    check_point_epoch_file = "{}_checkpoint_epoch.pth".format(model_prefix)
    if check_point and os.path.isfile(check_point_model_file) and os.path.isfile(check_point_epoch_file):
        start_epoch = torch.load(check_point_epoch_file)
        print "Restarting at epoch {} from {}".format(start_epoch+1, check_point_model_file)
        model.load_state_dict(torch.load(check_point_model_file))
    else:
        start_epoch = 0

    inputSpatialSize = torch.LongTensor(input_shape)

    draw_graph = True

    mlog = MeterLogger(nclass=nClasses, title="Sparse 3D UNet", server="cn4216")

    #Start clean
    for obj in gc.get_objects():
        try:
            if torch.is_tensor(obj) or (hasattr(obj, 'data') and torch.is_tensor(obj.data)):
                #print type(obj), obj.size()
                del obj
        except (SystemExit, KeyboardInterrupt):
            raise
        except Exception as e:
            pass

    for epoch in xrange(start_epoch, num_epochs):
        print "Epoch {}/{}".format(epoch, num_epochs - 1)
        print "-" * 10

        mlog.timer.reset()
        for phase in ['train', 'val']:
            datasets[phase].epoch = epoch
            num_batches = int(np.ceil(len(datasets[phase])/float(batch_size if phase == "train" else validation_batch_size)))

            if phase == 'train':
                scheduler.step()
                model.train(True)  # Set model to training mode
            else:
                model.train(False)  # Set model to evaluate mode

            #stats = ModelStats(phase)

            # Iterate over data.
            bar = tqdm(enumerate(dataloaders[phase]), total=num_batches, unit="batch", desc="Loading data", leave=True)
            for data_iter_num, data in bar:
                #Input will be turned SparseConvNetTensor
                #print "{} Batch: {} of {}".format(phase.title(), data_iter_num, num_batches-1),
                datasets[phase].batch = data_iter_num
                #print type(data["data"]), data["data"].__class__.__name__, data["data"].__class__.__name__ == "InputBatch"
                batch_weight = data.get("weight", None)
                print batch_weight
                if batch_weight is not None:
                    batch_weight = torch.from_numpy(batch_weight).float()
                    # if use_gpu:
                    #     batch_weight = batch_weight.cuda()
                sample_weights = data.get("sample_weights", None)
                if sample_weights is not None:
                    sample_weights = torch.from_numpy(sample_weights).float()
                    # if use_gpu:
                    #     sample_weights = sample_weights.cuda()

                #print sample_weights, batch_weight

                if data["data"].__class__.__name__ == "InputBatch":
                    sparse_input = True
                    inputs = data["data"]
                    labels = data["truth"]
                    if use_gpu:
                        inputs = inputs.cuda().to_variable(requires_grad=True)
                        labels = labels.cuda().to_variable()
                    else:
                        inputs = inputs.to_variable(requires_grad=True)
                        labels = labels.to_variable()

                elif isinstance(data["data"], (list, tuple)):
                    sparse_input = True
                    inputs = scn.InputBatch(3, inputSpatialSize)
                    labels = scn.InputBatch(3, inputSpatialSize)

                    if isinstance(data["data"][0], np.ndarray):
                        long_tensor = lambda arr: torch.from_numpy(arr).long()
                        float_tensor = lambda arr: torch.from_numpy(arr).float()
                    elif isinstance(data["data"][0], (list, tuple)):
                        long_tensor = lambda arr: torch.LongTensor(arr)
                        float_tensor = lambda arr: torch.FloatTensor(arr)
                    else:
                        raise RuntimeError("invalid datatype")

                    for sample, (indices, features, truth) in enumerate(izip(data["indices"], data["data"], data["truth"])):
                        inputs.addSample()
                        labels.addSample()

                        try:
                            indices = long_tensor(indices)
                            features = float_tensor(features)
                            truth = float_tensor(truth)
                        except RuntimeError as e:
                            print e
                            continue

                        try:
                            inputs.setLocations(indices, features, 0) #Use 1 to remove duplicate coords?
                            labels.setLocations(indices, truth, 0)
                        except AssertionError:
                            import pdb; pdb.set_trace()

                    del data
                    del indices
                    del truth

                    inputs.precomputeMetadata(1)

                    if use_gpu:
                        inputs = inputs.cuda()
                        labels = labels.cuda()

                    inputs = inputs.to_variable(requires_grad=True)
                    labels = labels.to_variable()

                elif isinstance(data["data"], torch.FloatTensor):
                    #Input is dense
                    print "Input is Dense"
                    sparse_input = False
                    if use_gpu:
                        inputs = inputs.cuda()
                        labels = labels.cuda()
                    inputs = Variable(data["data"], requires_grad=True)
                    inputs = scn.DenseToSparse(3)(inputs)
                    try:
                        inputs = inputs.cuda().to_variable(requires_grad=True)
                    except:
                        pass
                    labels = Variable(data["truth"])

                else:
                    raise RuntimeError("Invalid data from dataset")

                # zero the parameter gradients
                optimizer.zero_grad()

                # forward
                try:
                    outputs = model(inputs)
                except AssertionError:
                    print nFeatures, inputs
                    raise

                if sparse_input:
                    #criterion = DiceLoss(size_average=False)
                    use_size_average = False
                    weight = sample_weights if use_size_average else batch_weight

                    loss_fn = torch.nn.CrossEntropyLoss(weight=weight) #DiceLoss(size_average=use_size_average) #LossFunctions.dice_loss #(criterion()

                    loss = loss_fn(outputs, torch.max(labels.features, 1)[1]) #, inputs.getSpatialLocations(), weight) #.long())#.features, labels.features) #, inputs.getSpatialLocations(), scaling)

                    if draw_graph:
                        var_dot = dot.make_dot(loss) #, params=dict(model.named_parameters()))
                        var_dot.render('{}_{}_graph.pdf'.format(epoch, data_iter_num))
                        draw_graph = False
                        del var_dot

                    #stats.update(outputs.cpu(), labels.features.cpu(), loss.data[0], inputs.getSpatialLocations(), epoch, since)
                    #print loss
                    # if math.isnan(loss.data[0]):
                    #     print "Loss is Nan?"
                    #     import pdb; pdb. set_trace()
                else:
                    outputs = scn.SparseToDense(3, 1)(outputs)
                    criterion = DiceLoss(size_average=False)
                    loss = criterion(outputs.cpu(), labels.cpu()) #, inputs.getSpatialLocations(), scaling)
                    stats.update(outputs.data.cpu().view(-1), labels.data.cpu().view(-1), loss.data[0])

                mlog.update_loss(loss, meter='loss')
                mlog.update_meter(outputs, torch.max(labels.features, 1)[1], meters={'accuracy', 'map'})
                add_to_logger(mlog,  "Train" if phase=="train" else "Test", epoch, outputs, labels.features, batch_weight, n_classes=nClasses)

                # backward + optimize only if in training phase
                if phase == 'train':
                    a = list(model.parameters())[0].clone().data
                    loss.backward()
                    optimizer.step()
                    b = list(model.parameters())[0].clone().data
                    if torch.equal(a, b): print "NOT UPDATED"
                    del a
                    del b

                bar.set_description("{}: [{}][{}/{}]".format(phase, epoch, data_iter_num+1, num_batches))
                bar.set_postfix(
                    loss="{:.4f} ({:.4f})".format(mlog.meter["loss"].val, mlog.meter["loss"].mean),
                    dice_avg=mlog.meter["dice_avg"].val,
                    dice_class1=mlog.meter["dice_class1"].val,
                    weight_dice=mlog.meter["weighted_dice_wavg"].val,
                    refresh=False)
                bar.refresh()

                del inputs
                del labels
                del outputs
                del loss
                del loss_fn
                del batch_weight
                del sample_weights

                #Delete all unused objects on the GPU
                for obj in gc.get_objects():
                    try:
                        if torch.is_tensor(obj) or (hasattr(obj, 'data') and torch.is_tensor(obj.data)):
                            #print type(obj), obj.size()
                            del obj
                    except (SystemExit, KeyboardInterrupt):
                        raise
                    except Exception as e:
                        pass

            graph_logger(mlog, "Train" if phase=="train" else "Test", epoch)
            mlog.reset_meter(epoch, "Train" if phase=="train" else "Test")

            if check_point:
                torch.save(epoch, check_point_epoch_file)
                torch.save(model.state_dict(), check_point_model_file)

    #stats.plot_final()

    graph_logger(mlog, "Train" if phase=="train" else "Test", epoch, final=True)

    time_elapsed = time.time() - since
    print 'Training complete in {:.0f}m {:.0f}s'.format(time_elapsed/60, time_elapsed % 60)

    if save_final:
        torch.save(model.state_dict(), "{}.pth".format(model_prefix))

    return model

def parse_args():
    import argparse
    parser = argparse.ArgumentParser(description="Load data and truth files to train the 3dCNN")
    parser.add_argument(
        "--prefix",
        default=None)
    parser.add_argument(
        "-s",
        "--shape",
        type=int,
        nargs=3,
        default=(264,264,264))#(256,256,256)
    parser.add_argument(
        "--batch-size",
        type=int,
        default=16)
    parser.add_argument(
        "--epochs",
        type=int,
        default=100)
    parser.add_argument(
        "--learning-rate",
        type=float,
        default=0.001)
    parser.add_argument(
        "--learning-rate-drop",
        type=float,
        default=0.5)
    parser.add_argument(
        "--learning-rate-epochs",
        type=int,
        default=5)
    parser.add_argument(
        "--data-split",
        type=float,
        default=0.8)
    parser.add_argument(
        "--no-shuffle",
        default=False,
        action="store_true",
        help="Do not shuffle data")
    parser.add_argument(
        "--only-aa",
        default=False,
        action="store_true",
        help="Only use one feature: aa (20 features since aa is one hot encoded). Else use all 59 features.")
    parser.add_argument(
        "--only-atom",
        default=False,
        action="store_true",
        help="Only use one feature: atom type (5 features since atom is one hot encoded). Else use all 59 features.")
    parser.add_argument(
        "--non-geom-features",
        default=False,
        action="store_true",
        help="Only use non geometric features")
    parser.add_argument(
        "--use_deepsite_features",
        default=False,
        action="store_true",
        help="Only use DeepSite features")
    parser.add_argument(
        "--expand-atom",
        default=False,
        action="store_true",
        help="Expand atoms s.t. they take up voxels according to their spheres defined by their VDW radii.")
    parser.add_argument(
        "--course-grained",
        default=False,
        action="store_true",
        help="Validate the network using full protein rather than just the binding site"
    )
    parser.add_argument(
        "--no-batch-norm",
        default=False,
        action="store_true",
        help="Do not use BatchNorm after each conv layer"
    )
    parser.add_argument(
        "--use-resnet-unet",
        default=False,
        action="store_true",
        help="Do not use BatchNorm after each conv layer"
    )
    parser.add_argument(
        "--unclustered",
        default=False,
        action="store_true",
    )
    parser.add_argument(
        "--oversample",
        default=False,
        action="store_true",
        help="Over sample binding site atoms"
    )
    parser.add_argument(
        "--undersample",
        default=False,
        action="store_true",
        help="Undersample non bidning site atoms"
    )
    parser.add_argument(
        "--nFeatures",
        default=3,
        required=False,
        type=int,
        choices=range(3,8),
        metavar="[3-7]",
        help="Number of features to use -- only works in spherical mode"
    )
    parser.add_argument(
        "--allow-feature-combos",
        default=False,
        action="store_true",
        help="Allow combination of features -- only works in spherical mode"
    )
    parser.add_argument(
        "--dropout-width",
        default=False,
        action="store_true",
        help="Apply dropout after convolution operations on width"
    )
    parser.add_argument(
        "--dropout-depth",
        default=False,
        action="store_true",
        help="Apply dropout after convolution operations on depth"
    )
    parser.add_argument(
        "--dropout-p",
        default=0.5,
        type=float
    )
    parser.add_argument(
        "--bs-feature",
        default=None
    )
    parser.add_argument(
        "--bs-feature2",
        default=None
    )
    parser.add_argument(
        "--bs-features",
        default=None,
        nargs="+"
    )
    parser.add_argument(
        "--stripes",
        default=False,
        action="store_true",
        help="On spherical models, apply bs-feature2 as stripes on patch"
    )
    parser.add_argument(
        "--data-parallel",
        default=False,
        action="store_true",
        help=""
    )

    gpus = parser.add_mutually_exclusive_group()
    gpus.add_argument(
        "--num_gpus",
        type=int,
        default=1)
    gpus.add_argument(
        "--all_gpus",
        action="store_true",
        default=False)

    cpus = parser.add_mutually_exclusive_group()
    cpus.add_argument(
        "--num_cpus",
        type=int,
        default=1)
    cpus.add_argument(
        "--all_cpus",
        action="store_true",
        default=False)

    parser.add_argument(
        "ibis_data")

    args = parser.parse_args()

    if args.all_gpus:
        args.num_gpus = len(get_available_gpus())

    if args.all_cpus:
        args.num_cpus = None

    return args

if __name__ == "__main__":
    args = parse_args()

    train(
        args.ibis_data,
        input_shape           = args.shape,
        model_prefix          = args.prefix,
        only_aa               = args.only_aa,
        only_atom             = args.only_atom,
        non_geom_features     = args.non_geom_features,
        use_deepsite_features = args.use_deepsite_features,
        num_workers           = args.num_cpus,
        expand_atom           = args.expand_atom,
        num_epochs            = args.epochs,
        batch_size            = args.batch_size,
        shuffle               = not args.no_shuffle,
        use_gpu               = args.num_gpus > 0,
        initial_learning_rate = args.learning_rate,
        learning_rate_drop    = args.learning_rate_drop,
        learning_rate_epochs  = args.learning_rate_epochs,
        data_split            = args.data_split,
        course_grained        = args.course_grained,
        no_batch_norm         = args.no_batch_norm,
        use_resnet_unet       = args.use_resnet_unet,
        unclustered           = args.unclustered,
        undersample           = args.undersample,
        oversample            = args.oversample,
        nFeatures             = args.nFeatures,
        allow_feature_combos  = args.allow_feature_combos,
        bs_feature            = args.bs_feature,
        bs_feature2           = args.bs_feature2,
        bs_features           = args.bs_features,
        stripes               = args.stripes,
        data_parallel         = args.data_parallel,
        dropout_depth         = args.dropout_depth,
        dropout_width         = args.dropout_width,
        dropout_p             = args.dropout_p
    )