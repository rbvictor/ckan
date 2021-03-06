# encoding: utf-8

"""WSGI app initialization"""

import webob

from werkzeug.test import create_environ, run_wsgi_app
from wsgi_party import WSGIParty

from ckan.config.middleware.flask_app import make_flask_stack
from ckan.config.middleware.pylons_app import make_pylons_stack

import logging
log = logging.getLogger(__name__)

# This monkey-patches the webob request object because of the way it messes
# with the WSGI environ.

# Start of webob.requests.BaseRequest monkey patch
original_charset__set = webob.request.BaseRequest._charset__set


def custom_charset__set(self, charset):
    original_charset__set(self, charset)
    if self.environ.get('CONTENT_TYPE', '').startswith(';'):
        self.environ['CONTENT_TYPE'] = ''

webob.request.BaseRequest._charset__set = custom_charset__set

webob.request.BaseRequest.charset = property(
    webob.request.BaseRequest._charset__get,
    custom_charset__set,
    webob.request.BaseRequest._charset__del,
    webob.request.BaseRequest._charset__get.__doc__)

# End of webob.requests.BaseRequest monkey patch


def make_app(conf, full_stack=True, static_files=True, **app_conf):
    '''
    Initialise both the pylons and flask apps, and wrap them in dispatcher
    middleware.
    '''

    pylons_app = make_pylons_stack(conf, full_stack, static_files, **app_conf)
    flask_app = make_flask_stack(conf)

    app = AskAppDispatcherMiddleware({'pylons_app': pylons_app,
                                      'flask_app': flask_app})

    return app


class AskAppDispatcherMiddleware(WSGIParty):

    '''
    Establish a 'partyline' to each provided app. Select which app to call
    by asking each if they can handle the requested path at PATH_INFO.

    Used to help transition from Pylons to Flask, and should be removed once
    Pylons has been deprecated and all app requests are handled by Flask.

    Each app should handle a call to 'can_handle_request(environ)', responding
    with a tuple:
        (<bool>, <app>, [<origin>])
    where:
       `bool` is True if the app can handle the payload url,
       `app` is the wsgi app returning the answer
       `origin` is an optional string to determine where in the app the url
        will be handled, e.g. 'core' or 'extension'.

    Order of precedence if more than one app can handle a url:
        Flask Extension > Pylons Extension > Flask Core > Pylons Core
    '''

    def __init__(self, apps=None, invites=(), ignore_missing_services=False):
        # Dict of apps managed by this middleware {<app_name>: <app_obj>, ...}
        self.apps = apps or {}

        # A dict of service name => handler mappings.
        self.handlers = {}

        # If True, suppress :class:`NoSuchServiceName` errors. Default: False.
        self.ignore_missing_services = ignore_missing_services

        self.send_invitations(apps)

    def send_invitations(self, apps):
        '''Call each app at the invite route to establish a partyline. Called
        on init.'''
        PATH = '/__invite__/'
        for app_name, app in apps.items():
            environ = create_environ(path=PATH)
            environ[self.partyline_key] = self.operator_class(self)
            # A reference to the handling app. Used to id the app when
            # responding to a handling request.
            environ['partyline_handling_app'] = app_name
            run_wsgi_app(app, environ)

    def __call__(self, environ, start_response):
        '''Determine which app to call by asking each app if it can handle the
        url and method defined on the eviron'''
        # :::TODO::: Enforce order of precedence for dispatching to apps here.

        app_name = 'pylons_app'  # currently defaulting to pylons app
        answers = self.ask_around('can_handle_request', environ)
        log.debug('Route support answers for {0} {1}: {2}'.format(
            environ.get('REQUEST_METHOD'), environ.get('PATH_INFO'),
            answers))
        available_handlers = []
        for answer in answers:
            if len(answer) == 2:
                can_handle, asked_app = answer
                origin = 'core'
            else:
                can_handle, asked_app, origin = answer
            if can_handle:
                available_handlers.append('{0}_{1}'.format(asked_app, origin))

        # Enforce order of precedence:
        # Flask Extension > Pylons Extension > Flask Core > Pylons Core
        if available_handlers:
            if 'flask_app_extension' in available_handlers:
                app_name = 'flask_app'
            elif 'pylons_app_extension' in available_handlers:
                app_name = 'pylons_app'
            elif 'flask_app_core' in available_handlers:
                app_name = 'flask_app'

        log.debug('Serving request via {0} app'.format(app_name))
        environ['ckan.app'] = app_name
        return self.apps[app_name](environ, start_response)
