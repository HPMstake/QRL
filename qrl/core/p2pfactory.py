# coding=utf-8
import queue
import time
from collections import defaultdict

from google.protobuf.json_format import MessageToJson
from pyqrllib.pyqrllib import bin2hstr
from twisted.internet import reactor
from twisted.internet.protocol import ServerFactory

from qrl.core import config, logger
from qrl.core.p2pprotocol import P2PProtocol
from qrl.core.qrlnode import QRLNode

from qrl.generated import qrl_pb2


class P2PFactory(ServerFactory):
    protocol = P2PProtocol

    def __init__(self, buffered_chain, sync_state, node: QRLNode, pos=None):
        # FIXME: Constructor signature is not consistent with other factory classes
        self.master_mr = None
        self.pos = None
        self.buffered_chain = buffered_chain
        self.sync_state = sync_state
        self.stake = config.user.enable_auto_staking  # default to mining off as the wallet functions are not that responsive at present with it enabled..
        self.peers_blockheight = {}
        self.target_retry = defaultdict(int)
        self.target_peers = {}
        self.fork_target_peers = {}
        self.connections = 0
        self.buffer = ''
        self.sync = 0
        self.partial_sync = [0, 0]
        self.long_gap_block = 0
        self.mining = 0
        self.newblock = 0
        self.exit = 0
        self.genesis = 0
        self.missed_block = 0
        self.requested = [0, 0]
        self.ip_geotag = 1  # to be disabled in main release as reveals IP..
        self.last_reveal_one = None
        self.last_reveal_two = None
        self.last_reveal_three = None

        self.peer_connections = []

        self.node = node

        self.txn_processor_running = False

        self.bkmr_blocknumber = 0  # Blocknumber for which bkmr is being tracked
        self.bkmr_priorityq = queue.PriorityQueue()
        # Scheduled and cancel the call, just to initialize with IDelayedCall
        self.bkmr_processor = reactor.callLater(1, self.setPOS, pos=None)
        self.bkmr_processor.cancel()

        self.last_ping = None

    @property
    def height(self):
        return self.buffered_chain.height

    # factory network functions
    def setPOS(self, pos):
        self.pos = pos
        self.master_mr = self.pos.master_mr

    def RFM(self, data):
        """
        Request Full Message
        This function request for the full message against,
        the Message Receipt received.
        :return:
        """

        # FIXME: Again, breaking encasulation
        # FIXME: Huge amount of lookups in dictionaries

        msg_hash = data.hash

        if msg_hash in self.master_mr.hash_msg:
            if msg_hash in self.master_mr.requested_hash:
                del self.master_mr.requested_hash[msg_hash]
            return

        peers_list = self.master_mr.requested_hash[msg_hash].peers_connection_list
        message_request = self.master_mr.requested_hash[msg_hash]
        for peer in peers_list:
            if peer in message_request.already_requested_peers:
                continue
            message_request.already_requested_peers.append(peer)

            peer.transport.write(peer.wrap_message('SFM', MessageToJson(data)))
            call_later_obj = reactor.callLater(config.dev.message_receipt_timeout,
                                               self.RFM,
                                               data)
            message_request.callLater = call_later_obj
            return

        # If execution reach to this line, then it means no peer was able to provide
        # Full message for this hash thus the hash has to be deleted.
        # Moreover, negative points could be added to the peers, for this behavior
        if msg_hash in self.master_mr.requested_hash:
            del self.master_mr.requested_hash[msg_hash]

    def select_best_bkmr(self):
        blocknumber = self.bkmr_blocknumber
        try:
            dscore, dhash = self.bkmr_priorityq.get_nowait()
            if blocknumber <= self.buffered_chain.height:
                oldscore = self.buffered_chain.get_block_n_score(blocknumber)
                if dscore > oldscore:
                    del self.bkmr_priorityq
                    self.bkmr_priorityq = queue.PriorityQueue()
                    return

            data = qrl_pb2.MR()

            data.hash = dhash
            data.type = 'BK'

            self.RFM(data)
            self.bkmr_processor = reactor.callLater(5, self.select_best_bkmr)
        except queue.Empty:
            return
        except Exception as e:
            logger.error('select_best_bkmr Unexpected Exception')
            logger.error('%s', e)


    def get_block_n(self, n):
        logger.info('<<<Requested block: %s from peers.', n)
        for peer in self.peer_connections:
            peer.transport.write(self.protocol.wrap_message('BN', str(n)))
        return

    ##############################################
    # POS
    def send_st_to_peers(self, st):
        logger.info('<<<Transmitting ST: %s', st.activation_blocknumber)
        self.register_and_broadcast('ST', st.get_message_hash(), st.to_json())
        return

    def send_destake_txn_to_peers(self, destake_txn):
        logger.info('<<<Transmitting Destake Txn: %s', destake_txn.txfrom)
        self.register_and_broadcast('DST', destake_txn.get_message_hash(), destake_txn.to_json())
        return

    def send_block_to_peers(self, block):
        # logger.info('<<<Transmitting block: ', block.headerhash)
        data = qrl_pb2.MR()
        data.stake_selector = block.transactions[0].addr_from
        data.block_number = block.block_number
        data.prev_headerhash = bytes(block.prev_blockheaderhash)

        if block.block_number > 1:
            data.reveal_hash = block.reveal_hash

        self.register_and_broadcast('BK',
                                    block.headerhash,
                                    block.to_json(),
                                    data)
        return

    ##############################################
    # TRANSFERS
    def send_tx_to_peers(self, tx):
        logger.info('<<<Transmitting TX: %s', bin2hstr(tx.txhash))
        self.register_and_broadcast('TX', tx.get_message_hash(), tx.to_json())
        return

    ##############################################
    # NETWORK
    def ip_geotag_peers(self):
        logger.info('<<<IP geotag broadcast')
        for peer in self.peer_connections:
            peer.transport.write(self.protocol.wrap_message('IP'))
        return

    def ping_peers(self):
        logger.info('<<<Transmitting network PING')

        # FIXME: This last_ping seems obsolete
        self.last_ping = time.time()
        for peer in self.peer_connections:
            peer.transport.write(self.protocol.wrap_message('PING'))
        return

    ##############################################
    # GENERIC LOW-LEVEL
    def register_and_broadcast(self, msg_type, msg_hash: bytes, msg_json, data=None):
        # FIXME: Try to keep parameters in the same order (consistency)
        self.master_mr.register(msg_hash, msg_json, msg_type)

        # FIXME: Clean
        if not data:
            data = qrl_pb2.MR()

        data.hash = msg_hash
        data.type = msg_type

        self.broadcast(msg_hash, msg_type, data)

    def broadcast(self, msg_hash: bytes, msg_type, data=None):  # Move to factory
        """
        Broadcast
        This function sends the Message Receipt to all connected peers.
        :return:
        """
        ignore_peers = []
        if msg_hash in self.master_mr.requested_hash:
            ignore_peers = self.master_mr.requested_hash[msg_hash].peers_connection_list

        if not data:
            data = qrl_pb2.MR()
            data.hash = msg_hash
            data.type = msg_type

        for peer in self.peer_connections:
            if peer in ignore_peers:
                continue
            peer.transport.write(self.protocol.wrap_message('MR', MessageToJson(data)))

    # connection functions

    def reset_processor_flag(self, _):
        self.txn_processor_running = False

    def reset_processor_flag_with_err(self, msg):
        logger.error('Exception in txn task')
        logger.error('%s', msg)
        self.txn_processor_running = False

    # Event handlers
    # noinspection PyMethodMayBeStatic
    def clientConnectionLost(self, connector, reason):
        logger.debug('connection lost: %s', reason)
        # TODO: Reconnect has been disabled
        # connector.connect()

    # noinspection PyMethodMayBeStatic
    def clientConnectionFailed(self, connector, reason):
        logger.debug('connection failed: %s', reason)

    # noinspection PyMethodMayBeStatic
    def startedConnecting(self, connector):
        logger.debug('Started connecting: %s', connector)

    def connect_peers(self):
        """
        Will connect to all known peers. This is typically the entry point
        It does result in:
        - connectionMade in each protocol (session)
        - :py:meth:startedConnecting
        - :py:meth:clientConnectionFailed
        - :py:meth:clientConnectionLost
        :return:
        :rtype: None
        """
        logger.info('<<<Reconnecting to peer list: %s', self.node._peer_addresses)
        for peer_address in self.node._peer_addresses:
            # FIXME: Refactor search
            found = False
            for peer_conn in self.peer_connections:
                if peer_address == peer_conn.transport.getPeer().host:
                    found = True
                    break
            if found:
                continue
            reactor.connectTCP(peer_address, 9000, self)
