from astropy.coordinates import SkyCoord
import datetime
import galsim
from galsim import Image, InterpolatedImage, roman
import numpy as np
from lenstronomy.SimulationAPI.sim_api import SimAPI
from slsim.Observations import image_quality_lenstronomy
import os.path
import pickle
from webbpsf.roman import WFI

# NOTE: Adding sky background requires webbpsf-data, which can be found at
#       https://webbpsf.readthedocs.io/en/latest/installation.html. Then, the
#       environment variable "WEBBPSF_PATH" must be set to this webbpsf-data directory.
#       Additionally, the galsim module is required, which is not supported on Windows.

# NOTE: PSF convolution is very slow since the psf is being generated by
#       webbpsf. Alternatively, the user can download psfs from cached_webb_psf
#       (https://github.com/LSST-strong-lensing/data_public/webbpsf), where the
#       psfs have been generated ahead of time so that they can be loaded from
#       a file. The directory containing these psfs should be passed into the
#       "psf_directory" parameter below.


def simulate_roman_image(
    lens_class,
    band,
    num_pix,
    oversample=5,
    add_noise=True,
    with_source=True,
    with_deflector=True,
    detector=1,
    detector_pos=(2000, 2000),
    seed=42,
    ra=30,
    dec=-30,
    date=datetime.datetime(year=2027, month=7, day=7, hour=0, minute=0, second=0),
    psf_directory=None,
    **kwargs,
):
    """Creates an image of a selected lens with noise.

    :param lens_class: class object containing all information of the lensing system
        (e.g., Lens())
    :param band: imaging band
    :type band: string
    :param num_pix: number of pixels per axis
    :type num_pix: integer
    :param add_noise: determines whether sky background and detector effects are added or not
    :type add_background: bool
    :param with_source: determines whether source is included in image
    :type with_source: bool
    :param with_deflector: determines whether deflector is included in image
    :type with_deflector: bool
    :param detector: The specific Roman detector being used to generate the psf
    :type detector: integer from 1 to 18
    :param detector_pos: The position of the detector being used to generate the psf
    :type detector_pos: integer between 4 + num_pix * oversample and 4092 - num_pix * oversample
    :param seed: An rng seed used for generating detector effects in galsim
    :type seed: integer
    :param ra: Coordinate in space used to generate sky background
    :type ra: float between 15 and 45
    :param dec: Coordinate in space used to generate sky background
    :type dec: float between -45 and -15
    :param date: Date used to generate sky background
    :type date: datetime.datetime class
    :param psf_directory: Path to directory containing psf file(s) where the psf can be loaded.
                            Otherwise, the psf will be generated by webbpsf which is very slow
    :type psf_directory: string
    :param kwargs: additional keyword arguments for the bands
    :type kwargs: dict
    :return: simulated image
    :rtype: 2d numpy array
    """

    # Perform all operations with an additional 3 pixel buffer on each side
    # to avoid edge effects, cropped out at the end
    num_pix += 6

    kwargs_model, kwargs_params = lens_class.lenstronomy_kwargs(band)

    kwargs_single_band = image_quality_lenstronomy.kwargs_single_band(
        observatory="Roman", band=band, **kwargs
    )

    _exposure_time = kwargs_single_band["exposure_time"]

    # Unconvolved image will be drawn at oversampled pixel scale
    kwargs_single_band["pixel_scale"] /= oversample
    sim_api = SimAPI(
        numpix=num_pix * oversample,
        kwargs_single_band=kwargs_single_band,
        kwargs_model=kwargs_model,
    )
    kwargs_lens_light, kwargs_source, kwargs_ps = sim_api.magnitude2amplitude(
        kwargs_lens_light_mag=kwargs_params.get("kwargs_lens_light", None),
        kwargs_source_mag=kwargs_params.get("kwargs_source", None),
        kwargs_ps_mag=kwargs_params.get("kwargs_ps", None),
    )
    kwargs_numerics = {
        "point_source_supersampling_factor": 1,
        "supersampling_factor": 1,
    }
    image_model = sim_api.image_model_class(kwargs_numerics)

    kwargs_lens = kwargs_params.get("kwargs_lens", None)
    # Draws the unconvolved image
    array = _exposure_time * image_model.image(
        kwargs_lens=kwargs_lens,
        kwargs_source=kwargs_source,
        kwargs_lens_light=kwargs_lens_light,
        kwargs_ps=kwargs_ps,
        unconvolved=True,
        source_add=with_source,
        lens_light_add=with_deflector,
        point_source_add=True,
    )

    total_flux_cps = calculate_total_flux_cps(
        image_model, kwargs_source, kwargs_lens_light
    )

    # Converts image to the galsim InterpolatedImage class
    interp = InterpolatedImage(
        Image(array, xmin=0, ymin=0),
        scale=0.11 / oversample,
        flux=total_flux_cps * _exposure_time,
    )
    # Gets psf and convolve
    galsim_psf = get_psf(band, detector, detector_pos, oversample, psf_directory)
    convolved = galsim.Convolve(interp, galsim_psf)

    # Draw interpolated image at the original (not oversampled) pixel scale
    im = galsim.ImageF(num_pix, num_pix, scale=0.11)
    im.setOrigin(0, 0)
    image = convolved.drawImage(im)

    if add_noise:
        # Obtain sky background corresponding to certain band and add it to the image
        # Requires webbpsf data files to use
        image = add_roman_background(
            image, band, detector, num_pix, _exposure_time, ra, dec, date
        )

        # Add detector effects and get the resulting array
        rng = galsim.UniformDeviate(seed)
        roman.allDetectorEffects(
            image, prev_exposures=(), rng=rng, exptime=_exposure_time
        )

    array = image.array

    final_array = array[3:-3, 3:-3]
    final_array = final_array / _exposure_time

    return final_array


def calculate_total_flux_cps(image_model, kwargs_source, kwargs_lens_light):
    """
    :param image_model: Used to access the source model and lens light model's total flux
    :type image_model: Instance of ImageModel class
    :param kwargs_source: Contains source parameters, used to calculate source model total flux
    :type kwargs_source: dict
    :param kwargs_lens_light: Contains lens light parameters, used to calculate lens light model total flux
    :type kwargs_lens_light: dict
    :return: the total flux in the entire image in units of counts per second
    :rtype: float
    """

    flux_source = image_model.SourceModel.total_flux(kwargs_list=kwargs_source)
    total_flux_source = np.sum(flux_source)
    flux_lens_light = image_model.LensLightModel.total_flux(
        kwargs_list=kwargs_lens_light
    )
    total_flux_lens_light = np.sum(flux_lens_light)
    return total_flux_lens_light + total_flux_source


# The following functions have been copy-pasted from the mejiro repo
# Credit to Bryce Wedig


def get_psf(band, detector, detector_pos, oversample, psf_directory):
    """Obtain galsim psf corresponding to specific band, using webbpsf.

    :param band: The specific band corresponding to the psf
    :type band: string
    :param detector: The specific Roman detector being used to generate the psf
    :type detector: integer from 1 to 18
    :param detector_pos: The position of the detector being used to generate the psf
    :type detector_pos: integer between 4 + num_pix * oversample and 4092 - num_pix * oversample
    :param oversample: Number of times that each pixel's side is subdivided for higher accuracy psf convolution
    :type oversample: integer
    :param psf_directory: Path to directory containing psf file(s) where the psf can be loaded.
                            Otherwise, the psf will be generated by webbpsf which is very slow
    :type psf_directory: string
    :return: An image of the psf generated by webbpsf
    :rtype: galsim's InterpolatedImage class
    """
    detector = f"SCA{str(detector).zfill(2)}"
    # Since generating the webbpsf is very slow, it can alternatively be loaded from a pickle file
    # where the psf has been generated ahead of time
    psf_file_name = (
        f"{band}_{detector}_{detector_pos[0]}_{detector_pos[1]}_{oversample}.pkl"
    )
    if psf_directory is not None:
        psf_file_path = os.path.join(psf_directory, psf_file_name)
    else:
        psf_file_path = os.path.join(
            os.path.dirname(__file__), "..", "data", "webbpsf", psf_file_name
        )

    if os.path.exists(psf_file_path):
        with open(psf_file_path, "rb") as psf_file:
            psf = pickle.load(psf_file)
    else:
        wfi = WFI()
        wfi.filter = band.upper()
        wfi.detector = detector
        wfi.detector_position = detector_pos
        psf = wfi.calc_psf(oversample=oversample)

    # import PSF to GalSim
    oversampled_pixel_scale = 0.11 / oversample
    psf_image = galsim.Image(psf[0].data, scale=oversampled_pixel_scale)

    return galsim.InterpolatedImage(psf_image)


def add_roman_background(image, band, detector, num_pix, exposure_time, ra, dec, date):
    """Adds a sky and thermal background to image, corresponding to a specific band,
    detector, date, and coordinate in the sky.

    :param image: image to add the background to
    :type image: galsim Image class
    :param band: imaging band
    :type band: string
    :param detector: The specific Roman detector being used to generate the psf
    :type detector: integer from 1 to 18
    :param num_pix: number of pixels per axis
    :type num_pix: integer
    :param ra: Coordinate in space used to generate sky background
    :type ra: float between 15 and 45
    :param dec: Coordinate in space used to generate sky background
    :type dec: float between -45 and -15
    :param date: Date used to generate sky background
    :type date: datetime.datetime class
    :return: image with added background
    :rtype: galsim Image class
    """
    # Get bandpass object
    bandpass = get_bandpass(band)
    # Get wcs
    wcs_dict = _get_wcs_dict(ra, dec, date)
    wcs = wcs_dict[detector]

    # Build image
    sky_image = galsim.ImageF(num_pix, num_pix, wcs=wcs)
    sca_cent_pos = wcs.toWorld(sky_image.true_center)
    sky_level = roman.getSkyLevel(
        bandpass, world_pos=sca_cent_pos, exptime=exposure_time
    )
    sky_level *= 1.0 + roman.stray_light_fraction
    wcs.makeSkyImage(sky_image, sky_level)

    # Add thermal background
    thermal_bkg = roman.thermal_backgrounds[get_bandpass_key(band)] * exposure_time

    image = image + sky_image + thermal_bkg
    image.quantize()

    return image


def get_bandpass(band):
    """
    :param band: imaging band
    :type band: string
    :return: galsim bandpass object corresponding to specific band
    :rtype: galsim Bandpass class
    """
    bandpass_key = get_bandpass_key(band)
    return roman.getBandpasses()[bandpass_key]


def get_bandpass_key(band):
    """Translates the Roman bands to keys used in galsim.

    :param band: The Roman band to be translated
    :type band: string
    :return: Translated band
    :rtype: string
    """
    band = band.upper()
    translate = {
        "F062": "R062",
        "F087": "Z087",
        "F106": "Y106",
        "F129": "J129",
        "F158": "H158",
        "F184": "F184",
        "F146": "W149",
        "F213": "K213",
    }
    return translate[band]


def _get_wcs_dict(ra, dec, date):
    """
    :param ra: Coordinate in space used to generate sky background
    :type ra: float between 15 and 45
    :param dec: Coordinate in space used to generate sky background
    :type dec: float between -45 and -15
    :param date: Date used to generate sky background
    :type date: datetime.datetime class
    :return: WCS corresponding to date and coordinate in space
    :rtype: dictionary, where the keys are the detectors and the
        values are the WCS corresponding to each detector
    """

    skycoord = SkyCoord(ra, dec, frame="icrs", unit="deg")
    ra_hms, dec_dms = skycoord.to_string("hmsdms").split(" ")

    ra_targ = galsim.Angle.from_hms(ra_hms)
    dec_targ = galsim.Angle.from_dms(dec_dms)
    targ_pos = galsim.CelestialCoord(ra=ra_targ, dec=dec_targ)

    # NB targ_pos indicates the position to observe at the center of the focal plane array
    return roman.getWCS(world_pos=targ_pos, date=date)
