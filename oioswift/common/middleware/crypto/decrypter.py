# Copyright (c) 2015-2016 OpenStack Foundation
# Copyright (c) 2018-2019 OpenIO SAS
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from swift.common.swob import Request, HTTPException, HTTPForbidden
from swift.common.middleware.crypto import decrypter
from swift.common.request_helpers import get_container_update_override_key
from swift.common.utils import config_true_value
from swift.proxy.controllers.base import get_object_info


class DecrypterObjContext(decrypter.DecrypterObjContext):

    def get_decryption_keys(self, req, crypto_meta=None):
        """
        Determine if a response should be decrypted, and if so then fetch keys.

        :param req: a Request object
        :returns: a dict of decryption keys
        """
        if config_true_value(req.environ.get('swift.crypto.override')):
            self.logger.debug('No decryption is necessary because of override')
            return None

        info = get_object_info(req.environ, self.app, swift_source='DCRYPT')
        if 'crypto-etag' not in info['sysmeta']:
            # object is not cyphered
            return None

        key_id = crypto_meta.get('key_id') if crypto_meta else None
        try:
            return self.get_keys(req.environ, key_id=key_id)
        except HTTPException:
            # FIXME(FVE): check swift_source, accept if it is internal
            # FIXME(FVE): move that code to avoid printing an error
            if req.method in ('HEAD', 'GET'):
                try:
                    return self.get_keys(req.environ, ['container'],
                                         key_id=key_id)
                except HTTPException:
                    pass
                return None
            else:
                raise

    def decrypt_resp_headers(self, put_keys, post_keys):
        """
        Find encrypted headers and replace with the decrypted versions.

        :param put_keys: a dict of decryption keys used for object PUT.
        :param post_keys: a dict of decryption keys used for object POST.

        :return: A list of headers with any encrypted headers replaced by their
                 decrypted values.
        :raises HTTPInternalServerError: if any error occurs while decrypting
                                         headers
        :raises HTTPForbidden: if the decryption key is invalid
        """
        mod_hdr_pairs = []

        if put_keys:
            # Decrypt plaintext etag and place in Etag header for client
            # response
            etag_header = 'X-Object-Sysmeta-Crypto-Etag'
            encrypted_etag = self._response_header_value(etag_header)
            decrypted_etag = None
            if encrypted_etag and 'object' in put_keys:
                decrypted_etag = self._decrypt_header(
                    etag_header, encrypted_etag, put_keys['object'],
                    required=True)
                mod_hdr_pairs.append(('Etag', decrypted_etag))

            etag_header = get_container_update_override_key('etag')
            encrypted_etag = self._response_header_value(etag_header)
            if encrypted_etag and 'container' in put_keys:
                dcrypt_etag_override = self._decrypt_header(
                    etag_header, encrypted_etag, put_keys['container'])
                if decrypted_etag and dcrypt_etag_override != decrypted_etag:
                    self.app.logger.debug('Failed ETag verification')
                    raise HTTPForbidden('Invalid key')
                mod_hdr_pairs.append((etag_header, dcrypt_etag_override))
                # The real swift saves the cyphered ETag in the 'ETag' field,
                # whereas we store the ETag of the cyphered object.
                # The ETag of the cyphered object is of no use for previous
                # middlewares, so we replace it with the plaintext ETag.
                mod_hdr_pairs.append(('ETag', dcrypt_etag_override))

        # Decrypt all user metadata. Encrypted user metadata values are stored
        # in the x-object-transient-sysmeta-crypto-meta- namespace. Those are
        # decrypted and moved back to the x-object-meta- namespace. Prior to
        # decryption, the response should have no x-object-meta- headers, but
        # if it does then they will be overwritten by any decrypted headers
        # that map to the same x-object-meta- header names i.e. decrypted
        # headers win over unexpected, unencrypted headers.
        try:
            if post_keys:
                mod_hdr_pairs.extend(self.decrypt_user_metadata(post_keys))

            mod_hdr_names = {h.lower() for h, v in mod_hdr_pairs}
            mod_hdr_pairs.extend([(h, v) for h, v in self._response_headers
                                  if h.lower() not in mod_hdr_names])
        except KeyError:
            self.app.logger.debug('Not able to decrypt user metadata')
        return mod_hdr_pairs


class Decrypter(decrypter.Decrypter):
    """Middleware for decrypting data and user metadata."""

    def __call__(self, env, start_response):
        req = Request(env)
        try:
            parts = req.split_path(3, 4, True)
        except ValueError:
            return self.app(env, start_response)

        if parts[3] and req.method in ('HEAD', 'GET'):
            handler = DecrypterObjContext(self, self.logger).handle
        elif parts[2] and req.method == 'GET':
            handler = decrypter.DecrypterContContext(self,
                                                     self.logger).handle
        else:
            # url and/or request verb is not handled by decrypter
            return self.app(env, start_response)

        try:
            return handler(req, start_response)
        except HTTPException as err_resp:
            return err_resp(env, start_response)
