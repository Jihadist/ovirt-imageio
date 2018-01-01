

import httplib
import logging

import requests
from webob import exc

from http_helper import (
    addcors,
    authorize,
    success_codes as http_success_codes,
)
import config
import server

from ovirt_imageio_common import web


class RequestHandler(object):
    """
    Request handler for the /images/ resource.
    """

    def __init__(self, config, request):
        """
        :param config: config.py
        :param request: http request
        :return:
        """
        self.config = config
        self.request = request

    @addcors
    def options(self, res_id):
        return web.response(httplib.NO_CONTENT)

    @authorize
    @addcors
    def get(self, res_id):
        imaged_url = self.get_imaged_url(self.ticket)

        headers = self.get_default_headers(res_id)
        # Note that webob request.headers is case-insensitive.
        if 'Range' in self.request.headers:
            headers['Range'] = self.request.headers['Range']

        body = ""
        stream = True  # Don't let Requests read entire body into memory

        imaged_response = self.make_imaged_request(
            self.request.method, imaged_url, headers, body, stream)

        response = server.response(imaged_response.status_code)
        response.headers['Cache-Control'] = 'no-cache, no-store'
        response.headers['Content-Range'] = \
            imaged_response.headers.get('Content-Range', '')
        disposition = imaged_response.headers.get('Content-Disposition')
        if disposition is not None:
            response.headers['Content-Disposition'] = disposition


        max_transfer_bytes = int(imaged_response.headers.get('Content-Length'))
        response.body_file = web.CappedStream(RequestStreamAdapter(
            imaged_response.iter_content(4096, False)),
            max_transfer_bytes)
        response.headers['Content-Length'] = str(max_transfer_bytes)
        logging.debug("Resource %s: transferring %d bytes from host",
                      res_id, max_transfer_bytes)

        return response

    @authorize
    @addcors
    def put(self, res_id):
        """ Handles sending data to host for PUT or PATCH.
        :param request: http request object
        :type request: webob.Request
        :return: http response object
        :rtype: webob.Response
        """
        imaged_url = self.get_imaged_url(self.ticket)

        headers = self.get_default_headers(res_id)
        if 'Content-Length' not in self.request.headers:
            raise exc.HTTPBadRequest("Content-Length header is required")
        headers['Content-Length'] = self.request.headers['Content-Length']
        if 'Content-Range' in self.request.headers:
            headers['Content-Range'] = self.request.headers['Content-Range']

        max_transfer_bytes = int(headers['Content-Length'])
        body = web.CappedStream(self.request.body_file, max_transfer_bytes)
        stream = False
        logging.debug("Resource %s: transferring %d bytes to host",
                      res_id, max_transfer_bytes)
        imaged_response = self.make_imaged_request(
            self.request.method, imaged_url, headers, body, stream)

        response = server.response(imaged_response.status_code)
        response.headers['Cache-Control'] = 'no-cache, no-store'

        return response

    def get_imaged_url(self, ticket):
        return "{}/images/{}".format(ticket.url, ticket.id)

    def get_default_headers(self, resource_id):
        return {
            # accept-charset is only needed if you have query params
            'Cache-Control': 'no-cache',
            'X-AuthToken': resource_id,
        }

    def make_imaged_request(self, method, imaged_url, headers, body, stream):
        timeout = (self.config.imaged_connection_timeout_sec,
                   self.config.imaged_read_timeout_sec)

        logging.debug("Connecting to host at %s", imaged_url)
        logging.debug("Outgoing headers to host:\n" +
                      '\n'.join(('  {}: {}'.format(k, headers[k])
                                 for k in sorted(headers))))

        try:
            # TODO Pool requests, keep the session somewhere?
            # TODO Otherwise, we can use request.prepare()
            imaged_session = requests.Session()
            imaged_req = requests.Request(
                method, imaged_url, headers=headers, data=body)
            imaged_req.body_file = body
            # TODO log the request to vdsm
            imaged_prepped = imaged_session.prepare_request(imaged_req)
            imaged_resp = imaged_session.send(
                imaged_prepped, verify=config.engine_ca_cert_file,
                timeout=timeout, stream=stream)
        except requests.Timeout:
            s = "Timed out connecting to host"
            raise exc.HTTPGatewayTimeout(s)
        except requests.URLRequired:
            s = "Invalid host URI for host"
            raise exc.HTTPBadRequest(s)
        except requests.ConnectionError as e:
            s = "Failed communicating with host: " + e.__doc__
            logging.error(s, exc_info=True)
            raise exc.HTTPServiceUnavailable(s)
        except requests.RequestException as e:
            s = "Failed communicating with host: " + e.__doc__
            logging.error(s, exc_info=True)
            raise exc.HTTPInternalServerError(s)

        print imaged_resp.headers
        # logging.debug("Incoming headers from host:\n" +
        #               '\n'.join(('  {}: {}'
        #                          .format(k, imaged_resp.headers.get(k))
        #                          for k in sorted(imaged_resp.headers))))

        if imaged_resp.status_code not in http_success_codes:
            # Don't read the whole body, in case something went really wrong...
            s = next(imaged_resp.iter_content(256, False), "(empty)")
            logging.error("Failed: %s", s)
            # TODO why isn't the exception logged somewhere?
            raise exc.status_map[imaged_resp.status_code](
                "Failed response from host: {}".format(s))

        logging.debug(
            "Successful request to host: HTTP %d %s",
            imaged_resp.status_code,
            httplib.responses[imaged_resp.status_code]
        )
        return imaged_resp


class RequestStreamAdapter(object):
    """
    Converts Request.iter_content stream to have a standard read() interface
    """
    chunk_size = 4096

    def __init__(self, request_stream):
        self.generator = request_stream
        self.next_bytes = b''

    def read(self, size):
        ret = self.next_bytes
        while len(ret) < size:
            try:
                ret += self.generator.next()
            except StopIteration:
                break
        self.next_bytes = ret[size:]
        return ret[:size]
