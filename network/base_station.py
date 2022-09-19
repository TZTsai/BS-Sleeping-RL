from utils import *
from . import config
from .env_utils import *
from .user_equipment import UserEquipment, UEStatus
from visualize.obs import VisRolling
from traffic.config import numApps
from config import *


class ConnectMode(enum.IntEnum):
    Disconnect = -1
    Reject = 0
    Accept = 1


class BaseStation:
    total_antennas = config.totalAntennas
    tx_power = config.txPower
    bandwidth = config.bandWidth
    frequency = config.bsFrequency
    bs_height = config.bsHeight
    cell_radius = config.cellRadius
    num_conn_modes = len(ConnectMode)
    num_sleep_modes = len(config.sleepModeDeltas)
    num_ant_switch_opts = len(config.antennaSwitchOpts)
    wakeup_delays = config.wakeupDelays
    ant_switch_opts = config.antennaSwitchOpts
    ant_switch_energy = config.antSwitchEnergy
    sleep_switch_energy = config.sleepSwitchEnergy
    disconnect_energy = config.disconnectEnergy
    # power_alloc_weights = config.powerAllocWeights
    power_alloc_base = config.powerAllocBase
    buffer_shape = config.bufferShape
    buffer_chunk_size = config.bufferChunkSize
    buffer_num_chunks = config.bufferNumChunks
    include_action_pc = TRAIN
    bs_stats_dim = buffer_num_chunks * buffer_shape[1]
    ue_stats_dim = 12
    mutual_obs_dim = 5
    
    public_obs_space = make_box_env(
        [[0, total_antennas], [0, 1]] +
        [[0, 1]] * num_sleep_modes
    )
    private_obs_space = make_box_env(
        # [[0, 6], [0, 59], [0, 60]] +    # time
        [[0, 1]] * num_sleep_modes +    # next sleep mode
        [[0, 99]] +                     # wakeup time
        [[0, np.inf]] * bs_stats_dim +  # bs stats
        [[0, np.inf]] * ue_stats_dim    # ue stats
    )
    mutual_obs_space = make_box_env([[0, np.inf]] * mutual_obs_dim)
    self_obs_space = concat_box_envs(public_obs_space, private_obs_space)
    other_obs_space = concat_box_envs(public_obs_space, mutual_obs_space)
    total_obs_space = concat_box_envs(
        self_obs_space, duplicate_box_env(
            other_obs_space, config.numBS - 1))
    
    public_obs_ndims = box_env_ndims(public_obs_space)
    private_obs_ndims = box_env_ndims(private_obs_space)
    self_obs_ndims = box_env_ndims(self_obs_space)
    other_obs_ndims = box_env_ndims(other_obs_space)
    total_obs_ndims = box_env_ndims(total_obs_space)
    
    action_dims = (num_ant_switch_opts, num_sleep_modes, num_conn_modes)

    def __init__(
        self, id, pos, net, 
        ant_power=None, total_antennas=None,
        frequency=None, bandwidth=None,
    ):
        pos = np.asarray(pos)
        for k, v in locals().items():
            if v is not None and k != 'self':
                setattr(self, k, v)
        self.ues: Dict[int, UserEquipment] = dict()
        self.queue = deque()
        self.covered_ues = set()
        self._nb_dists = dict()
        self.reset()

    def reset(self):
        self.ues.clear()
        self.queue.clear()
        self.covered_ues.clear()
        self.sleep = 0
        self.conn_mode = 1
        self.num_ant = self.total_antennas
        self._power_alloc = None
        self._prev_sleep = 0
        self._next_sleep = 0
        self._pc = None
        self._time = 0
        self._timer = 0
        self._steps = 0
        self._wake_timer = 0
        self._wake_delay = 0
        self._arrival_rate = 0
        self._energy_consumed = 0
        # self._energy_consumed = defaultdict(float)
        self._sleep_time = np.zeros(self.num_sleep_modes)
        self._buffer = np.zeros(self.buffer_shape, dtype=np.float32)
        self._buf_idx = 0
        if EVAL:
            self._stats = defaultdict(float)
            self._stats.update(
                sleep_switches=np.zeros(self.num_sleep_modes))
            self._total_stats = defaultdict(float)
            self._total_stats.update(id=self.id)
            self.net.add_stat('bs', self._total_stats)
            self.update_stats()

    def update_stats(self):
        record = [self.power_consumption, self.cell_traffic_rate]
        self.insert_buffer(record)
        if EVAL:
            s = self._stats
            s['pc'] = record[0]
            s['tx_power'] = self.transmit_power
            s['n_ants'] = self.num_ant
            ue_stats = np.zeros((numApps, 6))
            for ue in self.ues.values():
                ue_stats[ue.app_type] += [1, ue.signal_power, ue.interference,
                                         ue.sinr, ue.data_rate, ue.required_rate]
            s['signal'] = div0(ue_stats[:, 1], ue_stats[:, 0])
            # s['interf'] = div0(ue_stats[:, 2].sum(), ue_stats[:, 0].sum())
            s['sinr'] = div0(ue_stats[:, 3], ue_stats[:, 0])
            s['sum_rates'] = div0(ue_stats[:, 4] / 1e6, ue_stats[:, 0])
            s['req_rates'] = div0(ue_stats[:, 5] / 1e6, ue_stats[:, 0])
            s['sum_rate'] = s['sum_rates'].sum()
            s['req_rate'] = s['req_rates'].sum()
            s['active_ues'] = len(self.ues)
            s['queued_ues'] = len(self.queue)
            s['idle_ues'] = len(self.covered_idle_ues)
            s['covered_ues'] = len(self.covered_ues)
            
            self._total_stats['steps'] += 1
            self._total_stats['time'] += self._timer
            self._total_stats['sleep_time'] += self._sleep_time
            for k, v in s.items():
                self._total_stats[k] += v

    def reset_stats(self):
        self._steps = 0
        self._timer = 0
        # self._disc_all = 0  # used to mark a disconnect_all action
        self._arrival_rate = 0
        self._energy_consumed = 0
        # self._energy_consumed.clear()
        
    ### properties ###
    
    @property
    def num_ue(self):
        return len(self.ues)
    
    @property
    def ues_full(self):
        return self.num_ue >= self.num_ant - 1
    
    @property
    def covered_idle_ues(self):
        return [ue for ue in self.covered_ues if ue.bs is None]
    
    @property
    def responding(self):
        return self.conn_mode > 0
    
    @property
    def transmit_power(self):
        return 0 if self.sleep else self.tx_power * self.num_ant
    
    @property
    def sum_rate(self):
        return sum(ue.data_rate for ue in self.ues.values()) / 1e6

    @property
    def power_alloc(self):
        if self._power_alloc is None:
            self.alloc_power()
        return self._power_alloc

    @property
    def operation_pc(self):  # operation power consumption
        if self._pc is None:
            self._pc = self.compute_power_consumption()
        return self._pc
    
    @property
    def power_consumption(self):
        return self._timer and self._energy_consumed / self._timer
        # sum(self._energy_consumed.values()) / self._timer
    
    @property
    def cell_traffic_rate(self):
        return self._steps and self._arrival_rate / self._steps / 1e6
    
    @property
    def wakeup_time(self):
        if self.sleep == self._next_sleep:
            return 0.
        else:
            return self._wake_delay - self._wake_timer
    
    ### actions ###
    
    @timeit
    def take_action(self, action):
        if not TRAIN:
            assert len(action) == len(self.action_dims)
            notice(f'BS {self.id} takes action:\n{action}')
        self.switch_antennas(int(action[0]))
        self.switch_sleep_mode(int(action[1]))
        self.switch_connection_mode(int(action[2]) - 1)
    
    def switch_antennas(self, opt):
        if DEBUG:
            assert opt in range(self.num_ant_switch_opts)
        num_switch = self.ant_switch_opts[opt]
        if num_switch == 0: return
        energy_cost = self.ant_switch_energy * abs(num_switch)
        if TRAIN:  # reduce number of antenna switches
            self.consume_energy(energy_cost, 'antenna')
        num_ant_new = self.num_ant + num_switch
        if num_ant_new <= self.num_ue or num_ant_new > self.total_antennas:
            return  # invalid action
        if EVAL:
            self._stats['ant_switches'] += abs(num_switch)
        self.num_ant = num_ant_new
        for ue in self.net.ues.values():
            ue.update_data_rate()
        debug(f'BS {self.id}: switched to {self.num_ant} antennas')
        self.update_power_allocation()

    def switch_sleep_mode(self, mode):
        if DEBUG:
            assert mode in range(self.num_sleep_modes)
        if mode == self.sleep:
            self._next_sleep = mode
            return
        if TRAIN:  # reduce number of sleep switches
            self.consume_energy(self.sleep_switch_energy[mode], 'sleep')
        # if mode == 3 and any(ue.status < 2 for ue in self.covered_ues):
        #     return  # cannot go to deep sleep if there are inactive UEs in coverage
        self._next_sleep = mode
        if mode > self.sleep:
            if DEBUG:
                info('BS {}: goes to sleep {} -> {}'.format(self.id, self.sleep, mode))
            if EVAL:
                self._stats['sleep_switches'][mode] += 1
            self._prev_sleep = self.sleep
            self.sleep = mode
        elif mode < self.sleep:
            self._wake_delay = self.wakeup_delays[self.sleep]

    def switch_connection_mode(self, mode):
        """
        Mode 0: disconnect all UEs and refuse new connections
        Mode 1: refuse new connections
        Mode 2: accept new connections
        Mode 3: accept new connections and take over all UEs in cell range
        """
        if DEBUG:
            assert mode in ConnectMode._member_map_.values()
        self.conn_mode = mode
        # if self.conn_mode > 0 and self.sleep > 2:  # cannot accept new connections in SM3
        #     self.consume_energy(2, 'connect')
        #     self.conn_mode = -1
        if self.conn_mode < 0:  # disconnect all ues and empty the queue
            self.disconnect_all()
        # elif mode == 2:  # take over all ues
        #     if self.sleep:  # cannot take over UEs if asleep
        #         self.consume_energy(2, 'connect')  # add EC penalty
        #     else:
        #         self.takeover_all()
    
    ### network functions ###
    
    def neighbor_dist(self, bs_id):
        if bs_id in self._nb_dists:
            return self._nb_dists[bs_id]
        bs = self.net.get_bs(bs_id)
        d = np.linalg.norm(self.pos - bs.pos) / 1000  # km
        self._nb_dists[bs_id] = d
        bs._nb_dists[self.id] = d
        return d

    def connect(self, ue):
        assert ue.bs is None
        self.ues[ue.id] = ue
        ue.bs = self
        ue.status = UEStatus.ACTIVE
        self.update_power_allocation()
        if DEBUG:
            debug('BS {}: connected UE {}'.format(self.id, ue.id))

    def _disconnect(self, ue_id):
        """ Don't call this directly. Use UE.disconnect() instead. """
        ue = self.ues.pop(ue_id)
        ue.bs = None
        ue.status = UEStatus.IDLE
        self.update_power_allocation()
        if DEBUG:
            debug('BS {}: disconnected UE {}'.format(self.id, ue_id))

    def respond_connection_request(self, ue):
        if EVAL:
            self._total_stats['num_requests'] += 1
        if self.responding:
            if DEBUG: assert ue.idle
            if self.sleep or self.ues_full:
                self.add_to_queue(ue)
            else:
                self.connect(ue)
            return True
        if EVAL:
            self._total_stats['num_rejects'] += 1

    def add_to_cell(self, ue):
        self.covered_ues.add(ue)
        self._arrival_rate += ue.required_rate
    
    def remove_from_cell(self, ue):
        self.covered_ues.remove(ue)
        if EVAL:
            self._total_stats['cell_traffic'] += ue.file_size
            self._total_stats['cell_dropped_traffic'] += max(0, ue.demand)

    # def takeover_all(self):
    #     if self.covered_ues and DEBUG:
    #         info(f'BS {self.id}: takes over all UEs in cell')
    #     for ue in self.covered_ues:
    #         if ue.bs is not self:
    #             if ue.bs is not None:
    #                 ue.disconnect()
    #                 self.consume_energy(self.disconnect_energy, 'disconnect')
    #             self.add_to_queue(ue)  # delay connection to the next step

    def add_to_queue(self, ue):
        assert ue.idle
        self.queue.append(ue)
        ue.bs = self
        ue.status = UEStatus.WAITING
        if DEBUG:
            debug('BS {}: added UE {} to queue'.format(self.id, ue.id))
        
    def pop_from_queue(self, ue=None):
        if ue is None:
            ue = self.queue.popleft()
        else:
            self.queue.remove(ue)
        ue.bs = None
        ue.status = UEStatus.IDLE
        if DEBUG:
            debug('BS {}: removed UE {} from queue'.format(self.id, ue.id))
        return ue
    
    ### state transition ###

    def update_power_allocation(self):
        self._power_alloc = None
        for ue in self.ues.values():
            ue.update_data_rate()
        self.update_power_consumption()

    def update_power_consumption(self):
        self._pc = None

    @timeit
    def alloc_power(self):
        if not self.ues: return
        if len(self.ues) > 1:
            r = np.array([ue.required_rate for ue in self.ues.values()]) / 1e7
            w = self.power_alloc_base ** np.minimum(r, 50.)
            # w *= np.sqrt(np.minimum(r / 1e7, 3.)) * 10
            # w = np.array([self.power_alloc_weights[ue.app_type]
            #               for ue in self.ues.values()])
            ps = self.transmit_power * w / w.sum()
        else:
            ps = [self.transmit_power]
        self._power_alloc = dict(zip(self.ues.keys(), ps))
        for ue in self.ues.values():
            ue.update_data_rate()
        if DEBUG:
            debug('BS {}: allocated power {}'.format(self.id, self._power_alloc))

    @timeit
    def update_sleep(self, dt):
        if EVAL:
            self._sleep_time[self.sleep] += dt
        if self._next_sleep == self.sleep:
            if self.queue and self.sleep in (1, 2):
                if DEBUG:
                    info('BS {}: automatically waking up'.format(self.id))
                self.switch_sleep_mode(0)
            elif self.sleep == 0 and not self.ues and self._prev_sleep == 1:
                if DEBUG:
                    info('BS {}: automatically goes to sleep'.format(self.id))
                self.switch_sleep_mode(1)
            return
        self._wake_timer += dt
        if self._wake_timer >= self._wake_delay:
            if DEBUG:
                info('BS {}: switched sleep mode {} -> {}'
                     .format(self.id, self.sleep, self._next_sleep))
            if EVAL:
                self._stats['sleep_switches'][self._next_sleep] += 1
            self._prev_sleep = self.sleep
            self.sleep = self._next_sleep
            self._wake_timer = 0.
        elif DEBUG:
            wake_time = (self._wake_delay - self._wake_timer) * 1000
            info('BS {}: switching sleep mode {} -> {} (after {:.0f} ms)'
                 .format(self.id, self.sleep, self._next_sleep, wake_time))

    @timeit
    def update_connections(self):
        if self.sleep:
            for ue in list(self.ues.values()):
                ue.disconnect()
                if TRAIN:  # reduce disconnections in the middle of service
                    self.consume_energy(self.disconnect_energy, 'disconnect')
                else:
                    self._stats['disconnects'] += 1
                if self.conn_mode >= 0:
                    self.add_to_queue(ue)
        else:
            while self.queue and not self.ues_full:
                ue = self.pop_from_queue()
                if self.conn_mode >= 0:
                    self.connect(ue)

    def disconnect_all(self):
        if DEBUG and (self.ues or self.queue):
            info('BS {}: disconnects {} UEs'.format(self.id, self.num_ue))
        for ue in list(self.ues.values()):
            ue.disconnect()
            if TRAIN:
                self.consume_energy(self.disconnect_energy, 'disconnect')
            else:
                self._stats['disconnects'] += 1
        while self.queue:
            self.pop_from_queue()

    @timeit
    def compute_power_consumption(
        self, eta=0.25, eps=8.2e-3, Ppa_max=config.maxPAPower,
        Psyn=1, Pbs=1, Pcd=1, Lbs=12.8, Tc=5000, Pfixed=18, C={},
        sleep_deltas=config.sleepModeDeltas
    ):
        """
        Reference: 
        Args:
        - eta: max PA efficiency of the BS
        - Ppa_max: max PA power consumption
        - Psyn: sync power
        - Pbs: power consumption of circuit components
        - Pcd: power consumption of coding/decoding
        
        Returns:
        The power consumption of the BS in Watts.
        """
        M = self.num_ant
        K = self.num_ue
        S = self.sleep
        R = 0
        if 'K3' not in C:
            B = self.bandwidth / 1e9
            # assume ET-PA (envelope tracking power amplifier)
            C['PA-fx'] = eps * Ppa_max / ((1 + eps) * eta)
            C['PA-ld'] = self.tx_power / ((1 + eps) * eta)
            C['K3'] = B / (3 * Tc * Lbs)
            C['MK1'] = (2 + 1/Tc) * B / Lbs
            C['MK2'] = 3 * B / Lbs
        Pnl = M * (C['PA-fx'] + Pbs) + Psyn + Pfixed  # no-load part of PC
        Pld = 0  # load-dependent part of PC
        if S:
            Pnl *= sleep_deltas[S]
        else:
            Pld = M * C['PA-ld']
            if K > 0:
                R = sum(ue.data_rate for ue in self.ues.values()) / 1e9
                Pld += Pcd*R + C['K3']*K**3 + M * (C['MK1']*K + C['MK2']*K**2)
        P = Pld + Pnl
        if EVAL:
            rec = dict(bs=self.id, M=M, K=K, R=R, S=S, Pnl=Pnl, Pld=Pld, P=P)
            self.net.add_stat('pc', rec)
            debug(f'BS {self.id}: {kwds_str(**rec)}')
        return P
    
    def consume_energy(self, e, k):
        self._energy_consumed += e
        # self._energy_consumed[k] += e
        self.net.consume_energy(e)

    def insert_buffer(self, record):
        self._buffer[self._buf_idx] = record
        self._buf_idx = (self._buf_idx + 1) % len(self._buffer)

    ### called by the environment ###
    
    def step(self, dt):
        self.update_sleep(dt)
        self.update_connections()
        self.consume_energy(self.operation_pc * dt, 'operation')
        self.update_timer(dt)

    @timeit
    @cache_obs
    def get_observation(self):
        obs = [self.observe_self()]
        for bs in self.net.bss.values():
            if bs is self: continue
            obs.append(bs.observe_self()[:self.public_obs_ndims])
            obs.append(self.observe_other(bs)[0])
        return np.concatenate(obs, dtype=np.float32)

    @timeit
    @cache_obs
    def observe_self(self):
        # hour, sec = divmod(self.net.world_time, 3600)
        # day, hour = divmod(self.net.world_time, 24)
        return np.concatenate([
            ### public information ###
            # self.pos
            # [self.band_width, self.transmit_power],
            [self.num_ant, self.responding],
            onehot_vec(self.num_sleep_modes, self.sleep),
            ### private information ###
            # [day % 7 + 1, hour, sec],
            onehot_vec(self.num_sleep_modes, self._next_sleep),
            [self.wakeup_time],
            self.get_bs_stats(),
            self.get_ue_stats()
        ], dtype=np.float32)

    @timeit
    @cache_obs
    def observe_other(self, bs):
        shared_ues = self.covered_ues & bs.covered_ues
        owned_ues = set(ue for ue in shared_ues if ue.bs is self)
        others_ues = set(ue for ue in shared_ues if ue.bs is bs)
        _, owned_thrp_req, owned_log_ratio = self.calc_sum_rate(owned_ues)
        _, others_thrp_req, others_log_ratio = self.calc_sum_rate(others_ues)
        obs = np.array([
            self.neighbor_dist(bs.id),
            # len(shared_ues), len(owned_ues), len(others_ues),
            owned_thrp_req, owned_log_ratio, 
            others_thrp_req, others_log_ratio
        ], dtype=np.float32)
        other_obs = obs[[0, 3, 4, 1, 2]]
        return obs, other_obs
    
    @staticmethod
    def calc_sum_rate(ues, kind=None):
        if kind is None or kind == 'actual':
            real_thrp = sum(ue.data_rate for ue in ues) / 1e6
            if kind: return real_thrp
        if kind is None or kind == 'required':
            required_thrp = sum(ue.required_rate for ue in ues) / 1e6
            if kind: return required_thrp
        if required_thrp > 0:
            required_thrp *= 1e-6
            if real_thrp == 0:
                return real_thrp, required_thrp, -5.
            real_thrp *= 1e-6
            ratio = np.clip(real_thrp / required_thrp, 1e-4, 1e4)
            return real_thrp, required_thrp, np.log10(ratio)
        else:
            real_thrp *= 1e-6
            return real_thrp, 0, 0

    # @VisRolling
    # @cache_obs
    def get_bs_stats(self):
        idx = [(self._buf_idx + i * self.buffer_chunk_size) % len(self._buffer)
               for i in range(self.buffer_num_chunks + 1)]
        chunks = np.array([self._buffer[i:j] if i < j else
                           np.vstack([self._buffer[i:], self._buffer[:j]])
                           for i, j in zip(idx[:-1], idx[1:])], dtype=np.float32)
        # yield chunks
        return chunks.mean(axis=1).reshape(-1)

    def get_ue_stats(self):
        thrp, thrp_req, log_ratio = self.calc_sum_rate(self.ues.values())
        thrp_req_queued = self.calc_sum_rate(self.queue, kind='required')
        idle_ues = self.covered_idle_ues
        thrp_req_idle = self.calc_sum_rate(idle_ues, kind='required')
        thrp_cell, thrp_req_cell, log_ratio_cell = self.calc_sum_rate(self.covered_ues)
        return [
            len(self.ues), len(self.queue), len(idle_ues), len(self.covered_ues),
            thrp, thrp_cell, log_ratio, log_ratio_cell,
            thrp_req, thrp_req_queued, thrp_req_idle, thrp_req_cell
        ]

    def update_timer(self, dt):
        self._steps += 1
        self._time += dt
        self._timer += dt

    @cache_obs
    def info_dict(self):
        ts = self._total_stats['sleep_time']
        infos = dict(
            conn_mode=self.conn_mode,
            sleep_mode=self.sleep,
            next_sleep=self._next_sleep,
            wakeup_time=self.wakeup_time,
            total_sleep_times=ts,
            avg_sleep_ratios=div0(ts, ts.sum())
        )
        infos.update(self._stats)
        steps = max(self._total_stats['steps'], 1)
        time = max(self._total_stats['time'], 1e-6)
        for k in self._stats:
            infos['avg_'+k] = self._total_stats[k] / steps
        infos['avg_reject_rate'] = div0(self._total_stats['num_rejects'],
                                    self._total_stats['num_requests'])
        infos['avg_cell_data_rate'] = self._total_stats['cell_traffic'] / time
        infos['avg_cell_drop_ratio'] = div0(self._total_stats['cell_dropped_traffic'],
                                        self._total_stats['cell_traffic'])
        infos['avg_sleep_switch_fps'] = self._total_stats['sleep_switches'] / time
        infos['avg_ant_switch_fps'] = self._total_stats['ant_switches'] / time
        return infos
    
    @classmethod
    def annotate_obs(cls, obs, trunc=None, keys=config.all_obs_keys):
        def squeeze_onehot(obs, i, j):
            if i >= len(obs): return obs
            return np.concatenate([
                obs[:i],
                np.argmax(obs[i:j], axis=0, keepdims=True), 
                obs[j:]])
        for i, key in enumerate(keys):
            if key.endswith('sleep_mode'):
                obs = squeeze_onehot(obs, i, i+cls.num_sleep_modes)
        if trunc is None:
            assert len(keys) == len(obs)
        else:
            keys = keys[:trunc]
        return dict(zip(keys, obs))

    def __repr__(self):
        return 'BS(%d)' % self.id
        # obs = self.annotate_obs(self.observe_self(), trunc=5)
        # return 'BS({})'.format(kwds_str(
        #     id=self.id, pos=self.pos, **obs
        # ))
