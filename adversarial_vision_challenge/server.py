from __future__ import absolute_import
from __future__ import print_function

import inspect
import os
from functools import wraps
from io import BytesIO
import timeit

import bson
import numpy as np
from flask import Flask, Response, request
from PIL import Image

# from werkzeug.exceptions import BadRequest
from werkzeug.exceptions import TooManyRequests

from . import __version__
from .logger import logger


# the number of max requests to predict for this model run
# based on the # of images to predict
number_of_max_predictions = int(os.environ.get('NUM_OF_IMAGES', 100)) * 1000
DEFAULT_BOUNDS = (0, 255)

def model_server(model):
    """Starts an HTTP server that provides access to a Foolbox model.

    Parameters
    ----------
    model : `foolbox.model.Model` instance
        The model that should be run.
    port : int
        The TCP port used by the HTTP server. Defaults to the PORT environment
        variable or 8989 if not set.

    """


    port = int(os.environ.get('PORT', default=8989))

    app = Flask(__name__)

    channel_axis = model.channel_axis()
    assert channel_axis in [1, 3]

    try:
        bounds = model.bounds()
    except AttributeError:
        bounds = DEFAULT_BOUNDS
        logger.info('model has no bounds method, assuming {}'.format(bounds))

    assert bounds == DEFAULT_BOUNDS, (
        'bounds must be {}, update your model or use the preprocessing '
        'argument of foolbox model wrappers'.format(DEFAULT_BOUNDS))

    def _predict(image):
        assert isinstance(image, np.ndarray)
        assert image.shape == (64, 64, 3)
        assert image.dtype == np.uint8

        # models (should) expect float32 arrays
        image = image.astype(np.float32)

        if channel_axis == 1:
            image = np.transpose(image, [2, 0, 1])

        prediction = model.predictions(image)

        if isinstance(prediction, np.ndarray) and prediction.size > 1:
            assert prediction.size == 200
            prediction = np.argmax(prediction)
        prediction = int(prediction)
        assert bounds[0] <= prediction < bounds[-1]
        return prediction

    _predict = _wrap(_predict, ['prediction'])

    @app.route("/")
    def main():  # pragma: no cover
        return Response(
            'NIPS 2018 Adversarial Vision Challenge Model Server\n',
            mimetype='text/plain')

    @app.route("/server_version", methods=['GET'])
    def server_version():
        v = __version__
        return Response(str(v), mimetype='text/plain')

    @app.route("/predict", methods=['POST'])
    def predict():
        _check_rate_limitation()
        start = timeit.default_timer()
        prediction = _predict(request)
        end = timeit.default_timer()
        logger.info('prediction took: %s s', (end - start))
        return prediction

    @app.route("/shutdown", methods=['GET'])
    def shutdown():
        _shutdown_server()
        return 'Shutting down ...'

    print('starting server on port {}'.format(port))
    app.run(host='0.0.0.0', port=port)


def _check_rate_limitation():
    global number_of_max_predictions
    logger.debug('Number of remaining max requests: %s',
                 number_of_max_predictions)
    number_of_max_predictions -= 1
    if (number_of_max_predictions < 0):
        logger.error('Maximal number of prediction requests exceeded: %s',
                     number_of_max_predictions)
        raise TooManyRequests(
            'Maximal number of prediction requests exceeded: {0}'.format(
                number_of_max_predictions))


def _shutdown_server():
    func = request.environ.get('werkzeug.server.shutdown')
    if func is None:  # pragma: no cover
        raise RuntimeError('Not running with the Werkzeug Server')
    func()


def _wrap(function, output_names):
    """A decorator that converts data between flask and python / numpy"""

    try:
        # Python 3
        sig = inspect.signature(function)
        params = sig.parameters
    except AttributeError:  # pragma: no cover
        # Python 2.7
        argspec = inspect.getargspec(function)
        params = dict(zip(argspec.args, [None] * len(argspec.args)))

    @wraps(function)
    def wrapper(request):
        verbose = request.args.get('verbose', False)

        if verbose:  # pragma: no cover
            print('headers', request.headers)
            print('args', list(request.args.keys()))
            print('form keys', list(request.form.keys()))
            print('files', list(request.files.keys()))
            print('is_json', request.is_json)
            print('data length', len(request.data))

        content_type = request.headers.get('content-type', '').lower()

        if content_type == 'application/bson':
            bson_args = bson.loads(request.data)
            bson_args = _decode_arrays(bson_args)

        else:  # pragma: no cover
            bson_args = {}

        args = {}

        def add_argument(name, value):
            if name in args:  # pragma: no cover
                print('ignoring {}, argument already exists'.format(name))
                return
            if name not in params:  # pragma: no cover
                print('ignoring {}, not accepted by function'.format(name))
                return
            args[name] = value

        for name, value in bson_args.items():
            add_argument(name, value)

        for name, value in request.args.items():  # pragma: no cover
            add_argument(name, value)

        for name, value in request.form.items():  # pragma: no cover
            add_argument(name, value)

        for name, value in request.files.items():  # pragma: no cover
            if name not in params:
                continue
            data = value.read()
            param = params[name]
            if param is not None and param.annotation == Image.Image:
                data = Image.open(BytesIO(data))
            add_argument(name, data)

        result = function(**args)
        if len(output_names) == 1:
            result = {output_names[0]: result}
        else:
            assert len(result) == len(output_names)
            result = dict(zip(output_names, result))
        result = _encode_arrays(result)
        result = bson.dumps(result)
        return Response(result, mimetype='application/bson')

    return wrapper


def _encode_arrays(d):
    for key in list(d.keys()):
        if isinstance(d[key], np.ndarray):
            array = d[key]
            d[key] = {
                'type': 'array',
                'shape': array.shape,
                'dtype': array.dtype.str,
                'data': array.tobytes(),
            }
    return d


def _decode_arrays(d):
    for key in list(d.keys()):
        if hasattr(d[key], 'get') \
                and d[key].get('type') == 'array':
            shape = d[key]['shape']
            dtype = d[key]['dtype']
            data = d[key]['data']
            array = np.frombuffer(data, dtype=dtype).reshape(shape)
            d[key] = array
    return d
