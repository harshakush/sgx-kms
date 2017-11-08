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
import os
import base64

from Crypto.PublicKey import DSA
from Crypto.PublicKey import RSA
from Crypto.Util import asn1
from cryptography import fernet
from oslo_config import cfg
import six

from barbican.common import config
from barbican.common import utils
from barbican import i18n as u
from barbican.plugin.crypto import crypto as c

from sgx import Secret, SGXInterface
import unicodedata

CONF = config.new_config()
LOG = utils.getLogger(__name__)

sgx_crypto_plugin_group = cfg.OptGroup(name='sgx_crypto_plugin',
                                          title="SGX Crypto Plugin Options")
sgx_crypto_plugin_opts = [
    cfg.StrOpt('kek',
               default='dGhpcnR5X3R3b19ieXRlX2tleWJsYWhibGFoYmxhaGg=',
               help=u._('Key encryption key to be used by SGX Crypto '
                        'Plugin'), secret=True)
]
CONF.register_group(sgx_crypto_plugin_group)
CONF.register_opts(sgx_crypto_plugin_opts, group=sgx_crypto_plugin_group)
config.parse_args(CONF)

_SIXTEEN_BYTE_KEY = 16

class SGXCryptoPlugin(c.CryptoPluginBase):
    """SGX based implementation of the crypto plugin."""

    def __init__(self, conf=CONF):
        LOG.info("Initializing SGX Crypto")
        self.master_kek = conf.sgx_crypto_plugin.kek
        dir_path = os.path.dirname(os.path.realpath(__file__))
        self.kek_file = os.path.join(dir_path, "masterkey")
        self.sealed_kek = None
        self.context = {}
        self.s_p_ctxt = {}
        self.c_p_net_ctxt = {}
        self.sgx = SGXInterface()
        self.sgx.init_env_variables()
        self.spid = self.sgx.get_spid()
        self.ias_crt = self.sgx.get_ias_crt()
        self.ias_enabled = self.sgx.get_ias_enable()
        self.enclave_id = self.sgx.init_enclave(self.sgx.barbie_s)
        self.client_verify_ias = False
        self.server_verify_ias = False

    def _get_master_kek(self):
        if self.sealed_kek:
            return self.sealed_kek
        if os.path.exists(self.kek_file):
            sealed_len = self._get_sealed_data_len(_SIXTEEN_BYTE_KEY)
            with open(self.kek_file, 'r') as f:
                kek = f.read()
            self.sealed_kek = Secret(kek, sealed_len)
        return self.sealed_kek

    def _get_sealed_data_len(self, plain_len):
        return self.sgx.barbie_c.get_sealed_data_len(self.enclave_id, 0, plain_len)

    def do_attestation(self, data, external_project_id, enc_keys, is_mutual):
        if is_mutual:
            return self._do_mutual_attestation(data, external_project_id, enc_keys)
        else:
            return self._do_attestation(data, external_project_id, enc_keys)

    def _do_attestation(self, data, external_project_id, enc_keys):
        """This method is used for attestation of the server enclave.
        The same method is used for provisioning master kek on server.
        It performs 3 operations based on the msg types in the data
        1. If no msg in data : Generate msg0 & msg1 and return
        2. If msg2 in data : Generate msg3 & return
        3. If msg4 in data : Return session key encrypted with dh key
                             Also returns session key encrypted with kek to be 
                             stored in db, if kek is provisioned.
                             If kek is not provisioned, sealed sk is returned
                             and stored in db which would be used in subsequent
                             provision kek call.
        """
        LOG.info("Call for Remote Attestation")

        response = {}
        output = None
        if 'msg2' in data:
            if not self.ias_enabled:
                if data['server_verify_ias'] and not data['client_verify_ias']:
                    response['status'] = 'Server is not configured to do IAS verification'
                else:
                    msg3, resp_crt, resp_sign, resp_body = self.get_msg3(data['msg2'],
                                                           self.context[external_project_id],
                                                           self.ias_crt, data['client_verify_ias'],
                                                           data['server_verify_ias'])
                    if resp_crt and resp_sign and resp_body :
                        response['resp_crt'] = resp_crt
                        response['resp_sign'] = resp_sign
                        response['resp_body'] = resp_body
                    response['msg3'] = msg3
                    response['status'] = 'OK'
                return response, output
            else :
                try:
                    msg3, resp_crt, resp_sign, resp_body = self.get_msg3(data['msg2'],
                                                           self.context[external_project_id],
                                                           self.ias_crt, data['client_verify_ias'],
                                                           True)
                    if resp_crt and resp_sign and resp_body :
                        response['resp_crt'] = resp_crt
                        response['resp_sign'] = resp_sign
                        response['resp_body'] = resp_body
                    response['msg3'] = msg3
                    response['status'] = 'OK'
                    return response, output
                except Exception as e:
                    response['status'] = str(e)
                    LOG.error(e, exc_info=True)
                    return response, output

        elif 'msg4' in data:
            project_id, project_id_len = self.get_project_id(data['msg4'],
                                                             self.context[external_project_id])
            if self.sgx.convert_to_python_data(project_id) == external_project_id:
                sealed_dh = self.get_dh_key(data['msg4'], self.context[external_project_id])
                try:
                    sealed_sk, sealed_mk = self._get_or_generate_enc_keys(project_id, enc_keys)
                except Exception as e:
                    LOG.error(e, exc_info=True)
                    response['status'] = 'Error in Remote Attestaion due to invalid input parameters'
                    return response, output
                sealed_len = self._get_sealed_data_len(_SIXTEEN_BYTE_KEY)
                dh_sk = self.sgx.transport(self.sgx.barbie_s, self.enclave_id,
                                           Secret(sealed_dh, sealed_len), sealed_sk, None)
                response['session_key'] = dh_sk
                response['status'] = 'OK'
                output = {}
                mk_sk = self.sgx.transport(self.sgx.barbie_s, self.enclave_id, sealed_mk, sealed_sk, project_id)
                output['sk'] = mk_sk
                output['mk'] = sealed_mk.value
                sealed_kek = self._get_master_kek()
                if sealed_kek:
                    kek_mk = self.sgx.transport(self.sgx.barbie_s, self.enclave_id, sealed_kek, sealed_mk, project_id)
                    output['mk'] = kek_mk
                    if 'policy' in data.keys():
                        output['policy'] = data['policy']
                        if 'mr_e_list' in data.keys():
                            mk_mr_e_list = self._process_mr_e_list(mk_sk, sealed_mk, data['mr_e_list'], external_project_id)
                            output['mk_mr_e_list'] = mk_mr_e_list
                        else:
                            output['mr_signer'] = data['mr_signer']
                return response, output
            else:
                response['status'] = 'Error in Attestaion due to Project ID miss match'
                return response, output

        else:
            msg0 = self.get_msg0(self.spid)
            response['msg0'] = msg0
            ctxt, msg1 = self.get_msg1()
            response['msg1'] = msg1
            self.context[external_project_id] = ctxt
            return response, output

    def get_msg0(self, spid=None):
        ret, msg0 = self.sgx.gen_msg0(self.sgx.barbie_s, spid)
        return msg0

    def get_msg1(self):
        return self.sgx.gen_msg1(self.sgx.barbie_s, self.enclave_id)

    def get_msg2(self, msg0, msg1, spid=None, client_verify_ias=False):
        ret, p_net_ctxt = self.sgx.proc_msg0(self.sgx.barbie_s, msg0, spid, client_verify_ias)
        msg2 = self.sgx.proc_msg1_gen_msg2(self.sgx.barbie_s, msg1, p_net_ctxt)
        return p_net_ctxt, msg2

    def get_msg3(self, msg2, p_ctxt, ias_crt=None, client_verify_ias=False, server_verify_ias=True):
        return self.sgx.proc_msg2_gen_msg3(self.sgx.barbie_s, self.enclave_id,
                msg2, p_ctxt, ias_crt, client_verify_ias, server_verify_ias)

    def get_msg4(self, msg3, p_net_ctxt, sealed_sk, project_id=None, ias_crt=None, client_verify_ias=False):
        return self.sgx.proc_msg3_gen_msg4(self.sgx.barbie_s, self.enclave_id,
                msg3, p_net_ctxt, sealed_sk, project_id, ias_crt, client_verify_ias)

    def proc_msg4(self, msg4, p_ctxt):
        status, sk = self.sgx.proc_msg4(self.sgx.barbie_s, self.enclave_id,
                msg4, p_ctxt)
        return sk

    def get_dh_key(self, msg4, ctxt):
        status, sealed_dh = self.sgx.get_dh_key(self.sgx.barbie_s, self.enclave_id,
                msg4, ctxt)
        return sealed_dh

    def get_project_id(self, msg4, p_ctxt):
        project_id, project_id_len = self.sgx.get_project_id(self.sgx.barbie_s, self.enclave_id,msg4, p_ctxt)
        return project_id, project_id_len

    def _do_mutual_attestation(self, data, external_project_id, enc_keys):
        LOG.info("Call for Mutual Attestation")

        response = {}
        output = None

        if all(msg in data for msg in ('c_msg0', 'c_msg1', 's_msg2')):
            try:
                if not self.ias_enabled:
                    if data['server_verify_ias'] and not data['client_verify_ias']:
                        response['status'] = 'Server is not configured to do IAS verification'
                        return response, output
                    else:
                        c_p_net_ctxt, c_msg2 = self.get_msg2(data['c_msg0'],
                                                             data['c_msg1'],
                                                             self.spid,
                                                             data['client_verify_ias'])
                        s_msg3, resp_crt, resp_sign, resp_body = self.get_msg3(data['s_msg2'],
                                             self.s_p_ctxt[external_project_id], self.ias_crt,
                                             data['client_verify_ias'], data['server_verify_ias'])
                        if resp_crt and resp_sign and resp_body :
                            response['s_resp_crt'] = resp_crt
                            response['s_resp_sign'] = resp_sign
                            response['s_resp_body'] = resp_body
                        response['s_msg3'] = s_msg3
                        response['c_msg2'] = c_msg2
                        self.client_verify_ias = data['client_verify_ias']
                        self.server_verify_ias = data['server_verify_ias']
                        self.c_p_net_ctxt[external_project_id] = c_p_net_ctxt
                        response['status'] = 'OK'
                        return response, output
                else:
                    c_p_net_ctxt, c_msg2 = self.get_msg2(data['c_msg0'],
                                                         data['c_msg1'],
                                                         self.spid,
                                                         data['client_verify_ias'])
                    s_msg3, resp_crt, resp_sign, resp_body = self.get_msg3(data['s_msg2'],
                                                       self.s_p_ctxt[external_project_id],
                                                       self.ias_crt, data['client_verify_ias'],
                                                       True)
                    if resp_crt and resp_sign and resp_body :
                        response['s_resp_crt'] = resp_crt
                        response['s_resp_sign'] = resp_sign
                        response['s_resp_body'] = resp_body
                    response['s_msg3'] = s_msg3
                    response['c_msg2'] = c_msg2
                    self.client_verify_ias = data['client_verify_ias']
                    self.server_verify_ias = data['server_verify_ias']
                    self.c_p_net_ctxt[external_project_id] = c_p_net_ctxt
                    response['status'] = 'OK'
                return response, output
            except Exception as e:
                LOG.error(e, exc_info=True)
                response['status'] = str(e)
                return response, output

        elif all(msg in data for msg in ('c_msg3', 's_msg4')):

            if all(response in data for response in ('c_resp_crt', 'c_resp_sign', 'c_resp_body')):
                crt, cacrt = self.sgx.get_crt(data['c_resp_crt'])
                try:
                    self.sgx.verify_certificate(crt, cacrt)
                    self.sgx.verify_signature(crt, data['c_resp_sign'], data['c_resp_body'])
                except Exception as e:
                    LOG.error(e, exc_info=True)
                    response['status'] = str(e)
                    return response, output
            project_id, project_id_len = self.get_project_id(data['s_msg4'],
                                                             self.s_p_ctxt[external_project_id])
            if self.sgx.convert_to_python_data(project_id) == external_project_id:
                sk = self.proc_msg4(data['s_msg4'], self.s_p_ctxt[external_project_id])
                try:
                    sealed_sk, sealed_mk = self._get_or_generate_enc_keys(project_id, enc_keys)
                except Exception as e:
                    LOG.error(e, exc_info=True)
                    response['status'] = 'Error in Mutual Attestaion due to invalid input parameters'
                if not self.ias_enabled and self.client_verify_ias:
                    response['status'] = 'Server is not configured to do IAS verification'
                    return response, output
                else:
                    try:
                        c_msg4 = self.get_msg4(data['c_msg3'],
                                        self.c_p_net_ctxt[external_project_id],
                                        sealed_sk, None, self.ias_crt, self.client_verify_ias)
                        response['c_msg4'] = c_msg4
                        response['status'] = 'OK'
                    except Exception as e:
                        LOG.error(e, exc_info=True)
                        response['status'] = 'IAS verification failed'
                        return response, output
                sealed_kek = self._get_master_kek()
                output = {}
                mk_sk = self.sgx.transport(self.sgx.barbie_s, self.enclave_id, sealed_mk, sealed_sk, project_id)
                output['sk'] = mk_sk
                kek_mk = self.sgx.transport(self.sgx.barbie_s, self.enclave_id, sealed_kek, sealed_mk, project_id)
                output['mk'] = kek_mk
                mr_e, mr_s = self.get_mr_enclave_signer(data['c_msg3'])
                output['mr_signer'] = mr_s
                output['mr_enclave'] = mr_e
                if 'policy' in data.keys():
                    output['policy'] = data['policy']
                    if 'mr_e_list' in data.keys():
                        mk_mr_e_list = self._process_mr_e_list(mk_sk, sealed_mk, data['mr_e_list'], external_project_id)
                        output['mk_mr_e_list'] = mk_mr_e_list

                return response, output
            else:
                response['status'] = 'Error in Mutual Attestaion due to Project ID miss match'
                return response, output

        else:
            s_msg0 = self.get_msg0(self.spid)
            response['s_msg0'] = s_msg0
            s_p_ctxt, s_msg1 = self.get_msg1()
            response['s_msg1'] = s_msg1
            self.s_p_ctxt[external_project_id] = s_p_ctxt
            response['status'] = 'OK'
            return response, output

    def _get_or_generate_enc_keys(self, project_id=None, enc_keys=None):
        sealed_sk = None
        sealed_mk = None
        if enc_keys:
            LOG.info("Using already created session key")
            sealed_kek = self._get_master_kek()
            if sealed_kek:
                mk = self.sgx.provision_kek(self.sgx.barbie_s, self.enclave_id, sealed_kek, enc_keys['mk'], project_id)
            else:
                mk = enc_keys['mk']
            sealed_len = self._get_sealed_data_len(_SIXTEEN_BYTE_KEY)
            sealed_mk = Secret(mk, sealed_len)
            sk = self.sgx.provision_kek(self.sgx.barbie_s, self.enclave_id, sealed_mk, enc_keys['sk'], project_id)
            sealed_sk = Secret(sk, sealed_len)
        else:
            LOG.info("Generating new session key")
            sealed_sk = self.sgx.generate_key(self.sgx.barbie_s, self.enclave_id, _SIXTEEN_BYTE_KEY)
            sealed_mk = self.sgx.generate_key(self.sgx.barbie_s, self.enclave_id, _SIXTEEN_BYTE_KEY)
        return sealed_sk, sealed_mk

    def _process_mr_e_list(self, mk_sk, sealed_mk, mr_e_list, project_id = None):
        separator = " "
        sealed_kek = self._get_master_kek()
        if not sealed_kek:
            raise Exception("Master key is not provisioned. Please contact administrator.")
        mk_mr_e_list = []
        mr_e_list = mr_e_list.split(separator)
        for mr_e in mr_e_list:
            mk_mr_e_list.append(self.sgx.kek_encrypt(self.enclave_id, mk_sk, sealed_mk, mr_e, project_id))
        mk_mr_e_list = separator.join(mk_mr_e_list)
        return mk_mr_e_list

    def get_mr_enclave_signer(self, msg_quote):
        mr_e = self.sgx.get_mr_enclave(msg_quote)
        mr_s = self.sgx.get_mr_signer(msg_quote)
        return mr_e, mr_s

    def compare_buffer(self, buffer1, buffer2, length):
        return self.sgx.compare_secret(self.sgx.barbie_s, buffer1, buffer2, length)

    def compare_mr_e_list(self, rec_mr_e, mk_mr_e_list, kek_mk, project_id):
        sealed_kek = self._get_master_kek()
        if not sealed_kek:
            raise Exception("Master key is not provisioned. Please contact administrator.")
        sealed_len = self._get_sealed_data_len(_SIXTEEN_BYTE_KEY)
        mk = self.sgx.provision_kek(self.sgx.barbie_s, self.enclave_id, sealed_kek, kek_mk, project_id)
        sealed_mk = Secret(mk, sealed_len)
        for mk_mr_e in mk_mr_e_list:
            mr_e = self.sgx.decrypt(self.sgx.barbie_s, self.enclave_id, sealed_mk, mk_mr_e)
            b64_mr_e = base64.b64decode(mr_e)
            if self.sgx.compare_secret(self.sgx.barbie_s, rec_mr_e, mr_e, 32):
                return True

    def do_provision_kek(self, data, external_project_id, enc_keys):
        LOG.info("In KEK Provisioning")
        sealed_len = self._get_sealed_data_len(_SIXTEEN_BYTE_KEY)
        sealed_sk, sealed_mk = self._get_or_generate_enc_keys(external_project_id, enc_keys)
        kek = self.sgx.provision_kek(self.sgx.barbie_s, self.enclave_id, sealed_sk, data['kek'], None)
        self.sealed_kek = Secret(kek, sealed_len)
        kek_mk = self.sgx.transport(self.sgx.barbie_s, self.enclave_id, self.sealed_kek, sealed_mk, external_project_id)
        with open(self.kek_file, 'w') as f:
            f.write(kek)
        return {"status" : "Ok"}, {'sk' : enc_keys['sk'], 'mk' : kek_mk}

    def encrypt(self, encrypt_dto, kek_meta_dto, project_id):
        project_id = unicodedata.normalize('NFKD', project_id).encode('ascii', 'ignore')
        unencrypted = encrypt_dto.unencrypted
        enc_keys = encrypt_dto.enc_keys
        if not isinstance(unencrypted, six.binary_type):
            raise ValueError(
                u._(
                    'Unencrypted data must be a byte type, but was '
                    '{unencrypted_type}'
                ).format(
                    unencrypted_type=type(unencrypted)
                )
            )
        sealed_kek = self._get_master_kek()
        if not sealed_kek:
            raise Exception("Master key is not provisioned. Please contact administrator.")
        mk_secret = None
        sealed_len = self._get_sealed_data_len(_SIXTEEN_BYTE_KEY)
        mk = self.sgx.provision_kek(self.sgx.barbie_s, self.enclave_id, sealed_kek, enc_keys['mk'], project_id)
        sealed_mk = Secret(mk, sealed_len)
        if enc_keys['sk']:
            LOG.info("Secret received in encrypted format")
            mk_secret = self.sgx.kek_encrypt(self.enclave_id, enc_keys['sk'], sealed_mk, unencrypted, project_id)
        else:
            LOG.info("Secret received in plain format")
            mk_secret = self.sgx.encrypt(self.sgx.barbie_s, self.enclave_id, sealed_mk, Secret(unencrypted, len(unencrypted)))

        return c.ResponseDTO(mk_secret, None)

    def decrypt(self, encrypted_dto, kek_meta_dto, kek_meta_extended,
                project_id):
        project_id = unicodedata.normalize('NFKD', project_id).encode('ascii', 'ignore')
        encrypted = encrypted_dto.encrypted
        enc_keys = encrypted_dto.enc_keys
        sealed_kek = self._get_master_kek()
        if not sealed_kek:
            raise Exception("Master key is not provisioned. Please contact administrator.")
        sk_secret = None
        sealed_len = self._get_sealed_data_len(_SIXTEEN_BYTE_KEY)
        mk = self.sgx.provision_kek(self.sgx.barbie_s, self.enclave_id, sealed_kek, enc_keys['mk'], project_id)
        sealed_mk = Secret(mk, sealed_len)
        if enc_keys['sk']:
            sk_secret = self.sgx.kek_decrypt(self.enclave_id, enc_keys['sk'], sealed_mk, encrypted, project_id)
        else:
            sk_secret = self.sgx.decrypt(self.sgx.barbie_s, self.enclave_id, sealed_mk, encrypted)
            sk_secret = base64.b64decode(sk_secret)
        return sk_secret


    def bind_kek_metadata(self, kek_meta_dto):
        kek_meta_dto.algorithm = 'aes'
        kek_meta_dto.bit_length = 128
        kek_meta_dto.mode = 'gcm'
        if not kek_meta_dto.plugin_meta:
            # the kek is stored encrypted in the plugin_meta field
            encryptor = fernet.Fernet(self.master_kek)
            key = fernet.Fernet.generate_key()
            kek_meta_dto.plugin_meta = encryptor.encrypt(key)
        return kek_meta_dto

    def generate_symmetric(self, generate_dto, kek_meta_dto, project_id):
        byte_length = int(generate_dto.bit_length) // 8
        sealed_secret = self.sgx.generate_key(self.sgx.barbie_s, self.enclave_id, byte_length)
        sealed_kek = self._get_master_kek()
        if not sealed_kek:
            raise Exception("Master key is not provisioned. Please contact administrator.")
        proj_id = unicodedata.normalize('NFKD', project_id).encode('ascii', 'ignore')
        kek_secret = self.sgx.transport(self.sgx.barbie_s, self.enclave_id, sealed_kek, sealed_secret, proj_id)
        return c.ResponseDTO(kek_secret, None)

    def generate_asymmetric(self, generate_dto, kek_meta_dto, project_id):
        """Generate asymmetric keys based on below rules:

        - RSA, with passphrase (supported)
        - RSA, without passphrase (supported)
        - DSA, without passphrase (supported)
        - DSA, with passphrase (not supported)

        Note: PyCrypto is not capable of serializing DSA
        keys and DER formated keys. Such keys will be
        serialized to Base64 PEM to store in DB.

        TODO (atiwari/reaperhulk): PyCrypto is not capable to serialize
        DSA keys and DER formated keys, later we need to pick better
        crypto lib.
        """
        if(generate_dto.algorithm is None or generate_dto
                .algorithm.lower() == 'rsa'):
            private_key = RSA.generate(
                generate_dto.bit_length, None, None, 65537)
        elif generate_dto.algorithm.lower() == 'dsa':
            private_key = DSA.generate(generate_dto.bit_length, None, None)
        else:
            raise c.CryptoPrivateKeyFailureException()

        public_key = private_key.publickey()

        # Note (atiwari): key wrapping format PEM only supported
        if generate_dto.algorithm.lower() == 'rsa':
            public_key, private_key = self._wrap_key(public_key, private_key,
                                                     generate_dto.passphrase)
        if generate_dto.algorithm.lower() == 'dsa':
            if generate_dto.passphrase:
                raise ValueError(u._('Passphrase not supported for DSA key'))
            public_key, private_key = self._serialize_dsa_key(public_key,
                                                              private_key)
        private_dto = self.encrypt(c.EncryptDTO(private_key),
                                   kek_meta_dto,
                                   project_id)

        public_dto = self.encrypt(c.EncryptDTO(public_key),
                                  kek_meta_dto,
                                  project_id)

        passphrase_dto = None
        if generate_dto.passphrase:
            if isinstance(generate_dto.passphrase, six.text_type):
                generate_dto.passphrase = generate_dto.passphrase.encode(
                    'utf-8')

            passphrase_dto = self.encrypt(c.EncryptDTO(generate_dto.
                                                       passphrase),
                                          kek_meta_dto,
                                          project_id)

        return private_dto, public_dto, passphrase_dto

    def supports(self, type_enum, algorithm=None, bit_length=None,
                 mode=None):
        if type_enum == c.PluginSupportTypes.ENCRYPT_DECRYPT:
            return True

        if type_enum == c.PluginSupportTypes.SYMMETRIC_KEY_GENERATION:
            return self._is_algorithm_supported(algorithm,
                                                bit_length)
        elif type_enum == c.PluginSupportTypes.ASYMMETRIC_KEY_GENERATION:
            return self._is_algorithm_supported(algorithm,
                                                bit_length)
        else:
            return False

    def _wrap_key(self, public_key, private_key,
                  passphrase):
        pkcs = 8
        key_wrap_format = 'PEM'

        private_key = private_key.exportKey(key_wrap_format, passphrase, pkcs)
        public_key = public_key.exportKey(key_wrap_format)

        return public_key, private_key

    def _serialize_dsa_key(self, public_key, private_key):

        pub_seq = asn1.DerSequence()
        pub_seq[:] = [0, public_key.p, public_key.q,
                      public_key.g, public_key.y]
        public_key = pub_seq.encode()

        prv_seq = asn1.DerSequence()
        prv_seq[:] = [0, private_key.p, private_key.q,
                      private_key.g, private_key.y, private_key.x]
        private_key = prv_seq.encode()

        return public_key, private_key

    def _is_algorithm_supported(self, algorithm=None, bit_length=None):
        """check if algorithm and bit_length combination is supported."""
        if algorithm is None or bit_length is None:
            return False

        if (algorithm.lower() in
                c.PluginSupportTypes.SYMMETRIC_ALGORITHMS and bit_length in
                c.PluginSupportTypes.SYMMETRIC_KEY_LENGTHS):
            return True
        elif (algorithm.lower() in c.PluginSupportTypes.ASYMMETRIC_ALGORITHMS
              and bit_length in c.PluginSupportTypes.ASYMMETRIC_KEY_LENGTHS):
            return True
        else:
            return False
 
