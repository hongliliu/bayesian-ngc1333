"""
The main sampling script.

Takes pixel positions (x, y) and the number of components (npeaks)
from the command line (via sys.argv[]), sets the priors, the likelihood
function, preforms some sanity checks, and fires up MultiNest through
pymultinest.
"""

import os
import sys
import mpi4py
import warnings
import numpy as np
from astropy import log
import astropy.units as u
from astropy.io import fits
# Those import warnings are annoying
with warnings.catch_warnings():
    warnings.simplefilter('ignore')
    #import corner
    import pymultinest
    # FIXME: cleanup ammonia wrapper file, why do we have corner kwargs in it?
    from pyspeckit.spectrum.models.ammonia import cold_ammonia_model
    from pyspecnest.ammonia import get_nh3_model, get_corner_kwargs
    from pyspeckit.spectrum.models.ammonia_constants import freq_dict
    from pyspecnest.chaincrunch import pars_xy, lnZ_xy, get_zero_evidence
# all the I/O functions now reside here
import opencube

# NOTE: change these lines into input arguments and you're good to go
#       Don't forget to edit the priors and the cube I/O!
lines = ['nh311', 'nh322']
line_names = ['oneone', 'twotwo']
npars = 6
npeaks = 2
name_id = 'ngc1333-gas'
proj_dir = os.path.expanduser('~/Projects/ngc1333-gas/')

# need the arguments below to save / load spectra
# (making a SpectralCube each time is too slow!)
iokwargs = dict(
    target_dir=os.path.join(proj_dir, 'nested-sampling/cubexarr'),
    target_xarr='{}-xarr.npy'.format(name_id),
    target_xarrkwargs='{}-xarrkwargs.p'.format(name_id),
    target_cubefile='{}-data.npy'.format(name_id),
    target_errfile='{}-errors.npy'.format(name_id),
    target_header='{}-header.p'.format(name_id),
    mmap_mode='r')

n_live_points = 400
sampling_efficiency = 0.8


def mpi_rank():
    """
    Returns the rank of the calling process.
    """
    comm = mpi4py.MPI.COMM_WORLD
    rank = comm.Get_rank()

    return rank

# pop culture references always deserve their own function
def i_am_root():
    """
    Checks if the running subprocess is of rank 0
    """
    try:
        return True if mpi_rank() == 0 else False
    except AttributeError:
        # not running MPI
        return True

try:
    npeaks = int(sys.argv[1])
except IndexError:
    npeaks = npeaks
    log.info("npeaks not specified, setting to {}".format(npeaks))

try:
    yx = int(sys.argv[2]), int(sys.argv[3])
except IndexError:
    yx = (140, 112)
    log.info("xy-pixel not specified, setting to {}".format(yx[::-1]))

try:
    plotting = bool(int(sys.argv[4]))
except IndexError:
    plotting = 0

if not plotting:
    plot_fit, plot_corner, show_fit, show_corner = False, False, False, False
else: # defaults a for non-batch run
    plot_fit = True
    plot_corner = True
    show_fit = True
    show_corner = True
    from chainconsumer import ChainConsumer
    import matplotlib.pyplot as plt
    plt.rc('text', usetex=True)


y, x = yx
sp = opencube.get_spectrum(x, y, **iokwargs)

fittype_fmt = 'cold_ammonia_x{}'
fitmodel = cold_ammonia_model
sp.specfit.Registry.add_fitter(fittype_fmt.format(npeaks), npars=npars,
                               function=fitmodel(line_names=line_names))
# is this still needed?
opencube.update_model(sp, fittype_fmt.format(npeaks))

# needed because of this:
# https://github.com/pyspeckit/pyspeckit/issues/179
sp.specfit.fitter.npeaks = npeaks
# npeaks > 1 seems to break because of fitter.parnames is not mirrored
if len(sp.specfit.fitter.parnames)==npars and npeaks > 1:
    sp.specfit.fitter.parnames *= npeaks

# special considerations on the priors:
#     - Temperatures: without a better knowledge, setting the priors to be as
#                     wide as we expect them to be - T_CMB to 25 (upper bound
#                     from the fact that we probably can't constrain it much
#                     further from the ammonia level population)
#     - Line widths: set the minimal FWHM to be velocity resolution, 0.07 km/s
sig2fwhm = 2*(2*np.log(2))**0.5
min_sigma = 0.07 / sig2fwhm
#   - Total ammonia column: ranging from dex 12 to 15, for a typical NH3
#       abundance of 1e-8 we're tracing H2 densities of 1e20 to 1e23
#   - Vlsr - from prior knowledge of cloud kinematics
dv_min, dv_max = 0.2, 3.0 # min/max separation of the velocity components
dxoff_prior = [dv_min, dv_max]
priors_xoff_transformed = (([[2.7315, 25], [2.7315, 25], [12.0, 15.0],
           [min_sigma, 1], dxoff_prior, [0.05, 2]])[::-1] * (npeaks - 1)
                          + [[2.7315, 25], [2.7315, 25], [12.0, 15.0],
           [min_sigma, 1], [3, 10], [0.05, 1]][::-1])
# TODO: make a wrapper function for pyspecnest instead!
priors = priors_xoff_transformed

nh3_model = get_nh3_model(sp, ['oneone', 'twotwo'], sp.error,
                          priors=priors, npeaks=npeaks, restrict_tex=True)

# Safeguard - check some common causes of failure before scheduling
# a job that would just throw tens of thousands of errors at us
no_valid_chans = not np.any(np.isfinite(nh3_model.ydata))
sanity_check = np.isfinite(
    nh3_model.log_likelihood([15, 5, 15, 0.2, 7, 0.5] * npeaks,
                             nh3_model.npars, nh3_model.dof))
if no_valid_chans or not sanity_check:
    # This should fail if, e.g., the errors are not finite
    log.error("no valid pixels at x={}; y={}. Aborting.".format(*yx[::-1]))
    sys.exit()

# The first process gets to make the directory structure!
output_dir = os.path.join(proj_dir, 'nested-sampling/')
fig_dir = os.path.join(output_dir, 'figs/')
suffix = 'x{1}y{0}'.format(*yx)
chains_dir = '{}chains/{}_{}'.format(output_dir, name_id, suffix)
if not os.path.exists(chains_dir):
    try: # hacks around a race condition
        os.makedirs(chains_dir)
    except OSError as e:
        if e.errno != 17:
            raise
        pass

chains_dir = '{}/{}-'.format(chains_dir, npeaks)

# Run MultiNest on the model+priors specified
pymultinest.run(nh3_model.xoff_symmetric_log_likelihood,
                nh3_model.prior_uniform, nh3_model.npars,
                outputfiles_basename=chains_dir,
                verbose=True, n_live_points=n_live_points,
                sampling_efficiency=sampling_efficiency)

# The remainder of the script is not essential for sampling, and can be safely
# moved out into a script of its own.
if i_am_root() and plot_fit:
    # parse the results as sensible output
    from pyspecnest.chaincrunch import analyzer_xy
    a = analyzer_xy(x, y, npeaks, output_dir=output_dir,
                    name_id=name_id, npars=npars)

    a_lnZ = a.get_stats()['global evidence']
    log.info('ln(Z) for model with {} line(s) = {:.1f}'.format(npeaks, a_lnZ))

    try:
        lnZ0 = fits.getdata('nested-sampling/ngc1333-Zs.fits')[0]
    except (FileNotFoundError, OSError) as e:
        cubes = opencube.make_cube_shh()
        lnZ0 = get_zero_evidence(data=cubes.cube, rms=cubes.errorcube,
                                 normalize=False)

    Zs = lnZ_xy(list(np.arange(npeaks+1)), x=x, y=y, output_dir=output_dir,
                name_id=name_id, silent=True, lnZ0=(lnZ0[y, x], 0))

    log.info('ln(Z{}/Z{}) = {:.2f}'.format(npeaks, npeaks-1,
                                        Zs[npeaks] - Zs[npeaks-1]))
    if npeaks > 1:
        log.info('ln(Z{}/Z{}) = {:.2f}'.format(npeaks, 0, Zs[npeaks] - Zs[0]))

if plot_fit and i_am_root():
    sp.plotter(errstyle='fill')

    mle_pars = pars_xy(x=x, y=y, npars=npars, npeaks=npeaks,
                       output_dir=output_dir, name_id=name_id)

    mle_parinfo = sp.specfit.fitter._make_parinfo(mle_pars, npeaks=npeaks)[0]

    try:
        sp.specfit.plot_fit(xarr=sp.xarr, pars=mle_parinfo,
                            show_components=True)
    except TypeError:
        # eh? does it want pars or parinfo?
        sp.specfit.plot_fit(xarr=sp.xarr, pars=mle_pars, show_components=True)

    # annotate the Bayes factors
    plt.annotate('ln(Z{}/Z{}) = {:.2f}'.format(npeaks, npeaks-1,
                 Zs[npeaks] - Zs[npeaks-1]), xy=(0.05, 0.90),
                 xycoords='axes fraction')
    if npeaks > 1:
        plt.annotate('ln(Z{}/Z{}) = {:.2f}'.format(npeaks, 0,
                     Zs[npeaks] - Zs[0]), xy=(0.05, 0.85),
                     xycoords='axes fraction')
    if show_fit:
        plt.show()

    fig_name = "{}-fit-{}-x{}".format(name_id, suffix, npeaks)
    plt.savefig(os.path.join(fig_dir, fig_name + ".pdf"))

if plot_corner and i_am_root():
    mle_multinest = pars_xy(x=x, y=y, npars=npars, npeaks=npeaks,
                            output_dir=output_dir, name_id=name_id)
    unfrozen_slice = nh3_model.get_nonfixed_slice(a.data.shape, axis = 1)
    c = ChainConsumer()
    parameters = nh3_model.get_names(latex=True, no_fixed=True)
    c.add_chain(a.data[:, 2:][unfrozen_slice], parameters=parameters)
    c.configure(statistics="max", summary=True)
    fig = c.plotter.plot(figsize="column")
    fig.get_size_inches()
    fig.set_size_inches(9, 7)

    fig_name = "{}-corner-{}-x{}".format(name_id, suffix, npeaks)
    plt.savefig(fig_dir + fig_name + ".pdf")

    if show_corner:
        plt.show()
