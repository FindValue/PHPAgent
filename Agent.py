#!/usr/bin/env python
# -*- coding: utf-8 -*-
__version__ = '0.1'
__author__ = "E"

import sys
import os
import re
import time
import errno
import binascii
import zlib
import struct
import random
import hashlib
import fnmatch
import logging
import configparser
import threading
import socket
import ssl
import select
import urllib.request
from urllib.parse import urlparse
import http.server
import socketserver

try:
    import ctypes
except ImportError:
    ctypes = None
try:
    import OpenSSL
except ImportError:
    OpenSSL = None
try:
    import ntlm
    import ntlm.HTTPNtlmAuthHandler
except ImportError:
    ntlm = None

class Common(object):
    '''global config module'''
    def __init__(self):
        configparser.RawConfigParser.OPTCRE = re.compile(r'(?P<option>[^=\s][^=]*)\s*(?P<vi>[=])\s*(?P<value>.*)$')
        self.CONFIG = configparser.ConfigParser()
        self.CONFIG.read('proxy.ini')
        #self.CONFIG.read(os.path.splitext(__file__)[0] + '.ini')

        self.LISTEN_VISIBLE = self.CONFIG.getint('listen', 'visible')


        self.PHP_ENABLE = self.CONFIG.getint('php', 'enable')
        self.PHP_IP = self.CONFIG.get('php', 'ip')
        self.PHP_PASSWORD = self.CONFIG.get('php', 'password').strip()
        self.PHP_PORT = self.CONFIG.getint('php', 'port')
        self.PHP_FETCHSERVER = self.CONFIG.get('php', 'fetchserver')
        self.PHP_FETCHSERVER_POST = self.CONFIG.get('php', 'fetchserverpost')

        self.FETCHMAX_LOCAL = self.CONFIG.getint('fetchmax', 'local') if self.CONFIG.get('fetchmax', 'local') else 3
        self.FETCHMAX_SERVER = self.CONFIG.get('fetchmax', 'server')

        self.AUTORANGE_HOSTS = tuple(self.CONFIG.get('autorange', 'hosts').split('|'))
        self.AUTORANGE_HOSTS_TAIL = tuple(x.rpartition('*')[2] for x in self.AUTORANGE_HOSTS)
        self.AUTORANGE_ENDSWITH = tuple(self.CONFIG.get('autorange', 'endswith').split('|'))
        self.AUTORANGE_MAXSIZE = self.CONFIG.getint('autorange', 'maxsize')

        self.USERAGENT_ENABLE = self.CONFIG.getint('useragent', 'enable')
        self.USERAGENT_STRING = self.CONFIG.get('useragent', 'string')

        self.PHP_FETCHSERVERS = self.PHP_FETCHSERVER.split(",")
        self.PHP_FETCHHOSTS = []

        for i in self.PHP_FETCHSERVERS : self.PHP_FETCHHOSTS.append(re.sub(':\d+$', '', urlparse(i).netloc))
        self.PHP_FETCHHOSTS_POST = re.sub(':\d+$', '', urlparse(self.PHP_FETCHSERVER_POST).netloc)
    def install_opener(self):
        handlers = [urllib.request.ProxyHandler({})]
        opener = urllib.request.build_opener(*handlers)
        opener.addheaders = []
        urllib.request.install_opener(opener)

    def info(self):
        info = ''
        info += '------------------------------------------------------\n'
        info += 'PHPAgent Version : %s (python/%s pyopenssl/%s)\n' % (__version__, sys.version.partition(' ')[0], (OpenSSL.version.__version__ if OpenSSL else 'Disabled'))
        info += 'PHP Mode Listen : %s:%d\n' % (self.PHP_IP, self.PHP_PORT) if self.PHP_ENABLE else ''
        info += 'PHP FetchServer : %s\n' % common.PHP_FETCHSERVER_POST if self.PHP_ENABLE else ''
        info += 'PHP FetchServer GET  : %s\n' % common.PHP_FETCHSERVERS if self.PHP_ENABLE else ''
        info += '------------------------------------------------------\n'
        return info

class CertUtil(object):
    '''CertUtil module, based on WallProxy 0.4.0'''

    CA = None
    CALock = threading.Lock()
    ca_vendor = 'PHPAgent'
    ca_digest = 'sha256'
    ca_validity_years = 10
    ca_validity = 24 * 60 * 60 * 365 * ca_validity_years

    @staticmethod
    def readFile(filename):
        content = None
        with open(filename, 'rb') as fp:
            content = fp.read()
        return content

    @staticmethod
    def writeFile(filename, content):
        with open(filename, 'wb') as fp:
            fp.write(content)

    @staticmethod
    def createKeyPair(bits=1024):
        pkey = OpenSSL.crypto.PKey()
        pkey.generate_key(OpenSSL.crypto.TYPE_RSA, bits)
        return pkey

    @staticmethod
    def createCertRequest(pkey, **subj):
        req = OpenSSL.crypto.X509Req()
        req.set_version(OpenSSL.SSL.SSLv3_METHOD)
        subject = req.get_subject()
        for k,v in subj.items():
            setattr(subject, k, v)
        req.set_pubkey(pkey)
        req.sign(pkey, CertUtil.ca_digest)
        return req

    @staticmethod
    def createCertificate(req, issuerKey, issuerCert, serial,notBefore, notAfter,extensions,sans=()):
        cert = OpenSSL.crypto.X509()
        cert.set_version(OpenSSL.SSL.SSLv3_METHOD)
        cert.set_serial_number(serial)
        cert.gmtime_adj_notBefore(notBefore)
        cert.gmtime_adj_notAfter(notAfter)
        cert.set_issuer(issuerCert.get_subject())
        cert.set_subject(req.get_subject())
        cert.set_pubkey(req.get_pubkey())
        if extensions :
            cert.add_extensions([OpenSSL.crypto.X509Extension(b'basicConstraints', False, b'CA:TRUE', subject=cert, issuer=cert)])
        else :
            val = ', '.join('DNS: %s' % x for x in sans).encode()
            cert.add_extensions([OpenSSL.crypto.X509Extension(b'subjectAltName', True, val)])
        cert.sign(issuerKey, CertUtil.ca_digest)
        return cert

    @staticmethod
    def loadPEM(pem, type):
        handlers = ('load_privatekey', 'load_certificate_request', 'load_certificate')
        return getattr(OpenSSL.crypto, handlers[type])(OpenSSL.crypto.FILETYPE_PEM, pem)

    @staticmethod
    def dumpPEM(obj, type):
        handlers = ('dump_privatekey', 'dump_certificate_request', 'dump_certificate')
        return getattr(OpenSSL.crypto, handlers[type])(OpenSSL.crypto.FILETYPE_PEM, obj)

    @staticmethod
    def makeCA():
        pkey = CertUtil.createKeyPair(bits=4096)
        subj = {'countryName': 'CN', 'stateOrProvinceName': 'Internet',
                'localityName': 'Cernet', 'organizationName': 'PHPAgent',
                'organizationalUnitName': 'PHPAgent Root', 'commonName': 'PHPAgent CA'}
        req = CertUtil.createCertRequest(pkey, **subj)
        cert = CertUtil.createCertificate(req, pkey, req, 0, 0, 60 * 60 * 24 * 7305, True)  #20 years
        return (CertUtil.dumpPEM(pkey, 0), CertUtil.dumpPEM(cert, 2))

    @staticmethod
    def get_cert_serial_number(host,cacrt):

        saltname = '%s|%s' % (cacrt.digest('sha1'), host)
        return int(hashlib.md5(saltname.encode('utf-8')).hexdigest(), 16)

    @staticmethod
    def makeCert(host, ca, sans=()):
        cakey = ca[0]
        cacrt = ca[1]
        if host[0] == '.':
            commonName = '*' + host
            organizationName = '*' + host
            sans = ['*' + host] + [x for x in sans if x != '*' + host]
        else:
            commonName = host
            organizationName = host
            sans = [host] + [x for x in sans if x != host]
        serial = CertUtil.get_cert_serial_number(host,cacrt)
        pkey = CertUtil.createKeyPair()
        subj = {'countryName': 'CN', 'stateOrProvinceName': 'Internet',
                'localityName': 'Cernet', 'organizationName': organizationName,
                'organizationalUnitName': 'PHPAgent Branch', 'commonName': commonName}
        req = CertUtil.createCertRequest(pkey, **subj)
        cert = CertUtil.createCertificate(req, cakey, cacrt, serial, 0, 60 * 60 * 24 * 7305, False, sans)
        return (CertUtil.dumpPEM(pkey, 0), CertUtil.dumpPEM(cert, 2))

    @staticmethod
    def getCertificate(host, sans=(), full_name=False):
        basedir = os.path.dirname(__file__)
        if host.count('.') >= 2 and [len(x) for x in reversed(host.split('.'))] > [2, 4] and not full_name:
            host = '.' + host.partition('.')[-1]

        keyFile = os.path.join(basedir, 'certs/%s.key' % host)
        crtFile = os.path.join(basedir, 'certs/%s.crt' % host)
        if os.path.exists(keyFile):
            return (keyFile, crtFile)
        if not os.path.isfile(keyFile):
            with CertUtil.CALock:
                key, crt = CertUtil.makeCert(host, CertUtil.CA)
                CertUtil.writeFile(keyFile, key)
                CertUtil.writeFile(crtFile, crt)
        return (keyFile, crtFile)

    @staticmethod
    def checkCA():
        #Check CA exists
        basedir = os.path.dirname(__file__)
        if not os.path.exists('certs') :
            os.mkdir('certs')
        keyFile = os.path.join(basedir, 'CA.key')
        crtFile = os.path.join(basedir, 'CA.crt')
        if not os.path.exists(keyFile) or not os.path.exists(crtFile) :
            if os.path.exists(keyFile):
                os.remove('CA.key')
            if os.path.exists(crtFile):
                os.remove('CA.crt')
            key, ca = CertUtil.makeCA()
            CertUtil.writeFile(keyFile, key)
            CertUtil.writeFile(crtFile, ca)
            [os.remove(os.path.join('certs', x)) for x in os.listdir('certs')]
        cakey = CertUtil.readFile(keyFile)
        cacrt = CertUtil.readFile(crtFile)
        CertUtil.CA = (CertUtil.loadPEM(cakey, 0), CertUtil.loadPEM(cacrt, 2))

class SimpleMessageClass(object):

    def __init__(self, fp, seekable=0):
        self.fp = fp
        self.dict = dict = {}
        self.linedict = linedict = {}
        self.headers = []
        headers_append = self.headers.append
        readline = fp.readline
        while 1:
            line = readline()
            if not line or line == '\r\n':
                break
            key, _, value = line.partition(':')
            key = key.lower()
            if value:
                dict[key] = value.strip()
                linedict[key] = line
                headers_append(line)

    def get(self, name, default=None):
        return self.dict.get(name.lower(), default)

    def items(self):
        return self.dict.items()

    def iterkeys(self):
        return self.dict.iterkeys()

    def itervalues(self):
        return self.dict.itervalues()

    def __getitem__(self, name):
        return self.dict[name.lower()]

    def __setitem__(self, name, value):
        key = name.lower()
        self.dict[key] = value
        self.linedict[key] = '%s: %s\r\n' % (name, value)
        self.headers = None

    def __delitem__(self, name):
        key = name.lower()
        del self.dict[key]
        del self.linedict[key]
        self.headers = None

    def __contains__(self, name):
        return name.lower() in self.dict

    def __len__(self):
        return len(self.dict)

    def __iter__(self):
        return iter(self.dict)

    def __str__(self):
        return ''.join(self.headers or self.linedict.itervalues())

class LocalProxyHandler(http.server.BaseHTTPRequestHandler):
    skip_headers = frozenset(['host', 'vary', 'via', 'x-forwarded-for', 'proxy-authorization', 'proxy-connection', 'upgrade', 'keep-alive'])
    SetupLock = threading.Lock()
    #MessageClass = SimpleMessageClass

    def socket_create_connection(self,host, port, timeout=None, source_address=None):
        logging.debug('socket_create_connection connect (%r, %r)', host, port)
        msg = 'getaddrinfo returns an empty list'
        for res in socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM):
            af, socktype, proto, canonname, sa = res
            sock = None
            try:
                sock = socket.socket(af, socktype, proto)
                if isinstance(timeout, (int, float)):
                    sock.settimeout(timeout)
                if source_address is not None:
                    sock.bind(source_address)
                sock.connect(sa)
                return sock
            except socket.error as msg:
                if sock is not None:
                    sock.close()
        raise (socket.error, msg)

    def socket_forward(local, remote, timeout=60, tick=2, bufsize=8192, maxping=None, maxpong=None, idlecall=None):
        timecount = timeout
        try:
            while 1:
                timecount -= tick
                if timecount <= 0:
                    break
                (ins, _, errors) = select.select([local, remote], [], [local, remote], tick)
                if errors:
                    break
                if ins:
                    for sock in ins:
                        data = sock.recv(bufsize)
                        if data:
                            if sock is local:
                                remote.sendall(data)
                                timecount = maxping or timeout
                            else:
                                local.sendall(data)
                                timecount = maxpong or timeout
                        else:
                            return
                else:
                    if idlecall:
                        try:
                            idlecall()
                        except Exception as e:
                            logging.exception('socket_forward idlecall fail:%s', e)
                        finally:
                            idlecall = None
        except Exception as ex:
            logging.exception('socket_forward error=%s', ex)
            raise
        finally:
            if idlecall:
                idlecall()

    def rangefetch(self, m, data):
        m = map(int, m.groups())
        start = m[0]
        end = m[2] - 1
        if 'range' in self.headers:
            req_range = re.search(r'(\d+)?-(\d+)?', self.headers['range'])
            if req_range:
                req_range = [u and int(u) for u in req_range.groups()]
                if req_range[0] is None:
                    if req_range[1] is not None:
                        if m[1] - m[0] + 1 == req_range[1] and m[1] + 1 == m[2]:
                            return False
                        if m[2] >= req_range[1]:
                            start = m[2] - req_range[1]
                else:
                    start = req_range[0]
                    if req_range[1] is not None:
                        if m[0] == req_range[0] and m[1] == req_range[1]:
                            return False
                        if end > req_range[1]:
                            end = req_range[1]
            data['headers']['content-range'] = 'bytes %d-%d/%d' % (start, end, m[2])
        elif start == 0:
            data['code'] = 200
            del data['headers']['content-range']
        data['headers']['content-length'] = end - start + 1
        partSize = common.AUTORANGE_MAXSIZE

        respline = '%s %d %s\r\n' % (self.protocol_version, data['code'], '')
        strheaders = ''.join('%s: %s\r\n' % ('-'.join(x.title() for x in k.split('-')), v) for k, v in data['headers'].items())
        self.wfile.write(respline + strheaders + '\r\n')

        if start == m[0]:
            self.wfile.write(data['content'])
            start = m[1] + 1
            partSize = len(data['content'])
        failed = 0
        logging.info('>>>>>>>>>>>>>>> Range Fetch started(%r)', self.headers.get('Host'))
        while start <= end:
            if failed > 5:
                break
            self.headers['Range'] = 'bytes=%d-%d' % (start, start + partSize - 1)
            retval, data = self.fetch(self.path, '', self.command, self.headers)
            if retval != 0 or data['code'] >= 400:
                failed += 1
                seconds = random.randint(2 * failed, 2 * (failed + 1))
                logging.error('rangefetch fail %d times: retval=%d http_code=%d, retry after %d secs!', failed, retval, data['code'] if not retval else 'Unkown', seconds)
                time.sleep(seconds)
                continue
            m = re.search(r'bytes\s+(\d+)-(\d+)/(\d+)', data['headers'].get('content-range',''))
            if not m or int(m.group(1)) != start:
                failed += 1
                continue
            start = int(m.group(2)) + 1
            logging.info('>>>>>>>>>>>>>>> %s %d' % (data['headers']['content-range'], end))
            failed = 0
            self.wfile.write(data['content'])
        logging.info('>>>>>>>>>>>>>>> Range Fetch ended(%r)', self.headers.get('Host'))
        return True

    def address_string(self):
        return '%s:%s' % (self.client_address[0], self.client_address[1])

    def send_response(self, code, message=None):
        self.log_request(code)
        message = message or self.responses.get(code, ('PHPAgent Notify',))[0]
        val = ('%s %d %s\r\n' % (self.protocol_version, code, message)).encode()
        self.wfile.write(val)

    def end_error(self, code, message=None, data=None):
        if not data:
            self.send_error(code, message)
        else:
            self.send_response(code, message)
            self.wfile.write(data)

    def do_CONNECT(self):
        return self.do_CONNECT_Thunnel()

    def do_CONNECT_Thunnel(self):
        # for ssl proxy
        host, _, port = self.path.rpartition(':')
        keyFile, crtFile = CertUtil.getCertificate(host)
        self.log_request(200)
        val = ('%s 200 OK\r\n\r\n' % self.request_version).encode()
        self.connection.sendall(val)
        try:
            self._realpath = self.path
            self._realrfile = self.rfile
            self._realwfile = self.wfile
            self._realconnection = self.connection
            self.connection = ssl.wrap_socket(self.connection, keyFile, crtFile, True)
            self.rfile = self.connection.makefile('rb', self.rbufsize)
            self.wfile = self.connection.makefile('wb', self.wbufsize)
            self.raw_requestline = self.rfile.readline()
            if self.raw_requestline == '':
                return
            self.parse_request()
            if self.path[0] == '/':
                self.path = 'https://%s%s' % (self._realpath, self.path)
                self.requestline = '%s %s %s' % (self.command, self.path, self.request_version)
            self.do_METHOD_Thunnel()
        except socket.error as e:
            logging.exception('do_CONNECT_Thunnel socket.error: %s', e)
        finally:
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except socket.error :
                try:
                    self.connection.close()
                    del self.connection
                except:
                    pass
            self.rfile = self._realrfile
            self.wfile = self._realwfile
            self.connection = self._realconnection

    def do_METHOD(self):
        return self.do_METHOD_Thunnel()

    def do_METHOD_Direct(self):
        scheme, netloc, path, params, query, fragment = urlparse.urlparse(self.path, 'http')
        try:
            host, _, port = netloc.rpartition(':')
            port = int(port)
        except ValueError:
            host = netloc
            port = 80
        try:
            self.log_request(200)
            idlecall = None
            sock = self.socket_create_connection((host, port))
            self.headers['Connection'] = 'close'
            data = '%s %s %s\r\n' % (self.command, urlparse.urlunparse(('', '', path, params, query, '')), self.request_version)
            data += ''.join('%s: %s\r\n' % (k, self.headers[k]) for k in self.headers if not k.startswith('proxy-'))
            data += '\r\n'
            content_length = int(self.headers.get('content-length', 0))
            if content_length > 0:
                data += self.rfile.read(content_length)
            sock.sendall(data)
            self.socket_forward(self.connection, sock, idlecall=idlecall)
        except Exception as ex:
            logging.exception('LocalProxyHandler.do_GET Error, %s', ex)
        finally:
            try:
                sock.close()
                del sock
            except:
                pass

    def do_METHOD_Thunnel(self):
        host = self.headers.get('Host') or urlparse.urlparse(self.path).netloc.partition(':')[0]
        if self.path[0] == '/':
            self.path = 'http://%s%s' % (host, self.path)
        payload_len = int(self.headers.get('content-length', 0))
        if payload_len > 0:
            payload = self.rfile.read(payload_len)
        else:
            payload = ''

        headers = ''.join('%s: %s\r\n' % (k, v) for k, v in self.headers.items() if k not in self.skip_headers)

        if host.endswith(common.AUTORANGE_HOSTS_TAIL):
            for pattern in common.AUTORANGE_HOSTS:
                if host.endswith(pattern) or fnmatch.fnmatch(host, pattern):
                    logging.debug('autorange pattern=%r match url=%r', pattern, self.path)
                    headers += 'range: bytes=0-%d\r\n' % common.AUTORANGE_MAXSIZE
                    break

        retval, data = self.fetch(self.path, payload, self.command, headers)
        try:
            if retval == -1:
                return self.end_error(502, str(data))
            code = data['code']
            headers = data['headers']
            self.log_request(code)
            if code == 206 and self.command == 'GET':
                m = re.search(r'bytes\s+(\d+)-(\d+)/(\d+)', headers.get('content-range',''))
                if m and self.rangefetch(m, data):
                    return
            content = '%s %d %s\r\n%s\r\n' % (self.protocol_version, code, self.responses.get(code, ('PHPAgent Notify', ''))[0], ''.join('%s: %s\r\n' % ('-'.join(x.title() for x in k.split('-')), v) for k, v in headers.items()))
            content = content.encode() + data['content']
            self.connection.sendall(content)
            if 'close' == headers.get('connection',''):
                self.close_connection = 1
        except socket.error as err:#(err, _):
            import traceback
            print(traceback.print_exc())
                
            # Connection closed before proxy return
            if err in (10053, errno.EPIPE):
                return

class PHPProxyHandler(LocalProxyHandler):

    comm = 0

    def urlfetch(self,url, payload, method , headers, fetchhost, fetchserver):
        errors = []
        p = url.split('?')
        if (len(p) == 2):
            params = {'url': p[0], 'method': method, 'headers': headers, 'payload': payload, 'requestparams' : p[1]}
        else:
            params = {'url': url, 'method': method, 'headers': headers, 'payload': payload}
        logging.info('urlfetch params %s', params)
        if common.PHP_PASSWORD:
            params['password'] = common.PHP_PASSWORD
        if common.FETCHMAX_SERVER:
            params['fetchmax'] = common.FETCHMAX_SERVER
        if common.USERAGENT_ENABLE:
            params['useragent'] = common.USERAGENT_STRING
        params = '&'.join('%s=%s' % (k, bytes.decode(binascii.b2a_hex(v.encode()))) for k, v in params.items())
        logging.info('urlfetch=== params %s', params)
        #params = zlib.compress(params.encode(), 3)
        params = params.encode()
        for i in range(common.FETCHMAX_LOCAL):
            try:
                logging.debug('urlfetch %r by %r', url, fetchserver)
                request = urllib.request.Request(fetchserver, params)
                response = urllib.request.urlopen(request)
                data = response.read()
                response.close()
                logging.info('urlfetch=== data %s', data[0])
                if data[0] == 48:#'0'
                    raw_data = data[1:]
                elif data[0] == 49:#'1'
                    raw_data = zlib.decompress(data[1:])
                else:
                    raise ValueError('Data format not match(%s)' % url)
                data = {}
                data['code'], hlen, clen = struct.unpack('>3I', raw_data[:12])
                tlen = 12 + hlen + clen
                realtlen = len(raw_data)
                if realtlen == tlen:
                    data['content'] = raw_data[12 + hlen:]
                elif realtlen > tlen:
                    data['content'] = raw_data[12 + hlen:tlen]
                else:
                    raise ValueError('Data length is short than excepted!')
                raw_data = (raw_data[12:12+hlen]).decode()
                raw_data = raw_data.split('&')
                data['headers'] = dict((k, binascii.a2b_hex(v).decode()) for k, _, v in (x.partition('=') for x in raw_data))
                return (0, data)
            except Exception as e:
                #import traceback
                #print(traceback.print_exc())
                logging.error('%s fetch error=%s', url, str(e))
                errors.append(str(e))
                time.sleep(i + 1)
                continue
        return (-1, errors)

    def fetch(self, url, payload, method='GET', headers=''):
        logging.info('urlfetch headers=%s',str(headers))
        headers = str(headers)
        if method == None:
            method = 'GET'
        if method == 'GET':
            PHPProxyHandler.comm = (PHPProxyHandler.comm + 1) % phpLength
            logging.info('urlfetch method=%s',method)
            return self.urlfetch(url, payload, method, headers, common.PHP_FETCHHOSTS[PHPProxyHandler.comm], common.PHP_FETCHSERVERS[PHPProxyHandler.comm])
        else :
            return self.urlfetch(url, payload, method, headers, common.PHP_FETCHHOSTS_POST, common.PHP_FETCHSERVER_POST)

    def setup(self):
        PHPProxyHandler.do_CONNECT = LocalProxyHandler.do_CONNECT_Thunnel
        PHPProxyHandler.do_GET = LocalProxyHandler.do_METHOD_Thunnel
        PHPProxyHandler.do_POST = LocalProxyHandler.do_METHOD_Thunnel
        PHPProxyHandler.do_PUT = LocalProxyHandler.do_METHOD_Thunnel
        PHPProxyHandler.do_DELETE = LocalProxyHandler.do_METHOD_Thunnel
        PHPProxyHandler.do_ET = LocalProxyHandler.do_METHOD_Thunnel
        PHPProxyHandler.do_HEAD = LocalProxyHandler.do_METHOD_Thunnel
        PHPProxyHandler.do_PATCH = LocalProxyHandler.do_METHOD_Thunnel
        PHPProxyHandler.do_OPTIONS = LocalProxyHandler.do_METHOD_Thunnel
        PHPProxyHandler.do_TRACE = LocalProxyHandler.do_METHOD_Thunnel
        PHPProxyHandler.setup = http.server.BaseHTTPRequestHandler.setup
        http.server.BaseHTTPRequestHandler.setup(self)

class LocalProxyServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

logging.basicConfig(level=logging.ERROR, format='%(levelname)s - - %(asctime)s %(message)s', datefmt='[%d/%b/%Y %H:%M:%S]')
common = Common()
rand = random.randint(0, len(common.PHP_FETCHSERVERS) - 1)
phpLength = len(common.PHP_FETCHSERVERS)

def main():
    if not OpenSSL:
        logging.critical('OpenSSL is disabled, ABORT!')
        sys.exit(-1)
    CertUtil.checkCA()
    common.install_opener()
    sys.stdout.write(common.info())
    httpd = LocalProxyServer((common.PHP_IP, common.PHP_PORT), PHPProxyHandler)
    httpd.serve_forever()

if __name__ == '__main__':
    main()
