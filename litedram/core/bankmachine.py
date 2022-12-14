#
# This file is part of LiteDRAM.
#
# Copyright (c) 2015 Sebastien Bourdeauducq <sb@m-labs.hk>
# Copyright (c) 2016-2019 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""LiteDRAM BankMachine (Rows/Columns management)."""

import math

from migen import *

from litex.soc.interconnect import stream
from litex.soc.interconnect.csr import *

from litedram.common import *
from litedram.core.multiplexer import *
from litedram.core.logging import *

from functools import reduce
from operator import or_, and_

# AddressSlicer ------------------------------------------------------------------------------------

class _AddressSlicer:
    """Helper for extracting row/col from address

    Column occupies lower bits of the address, row - higher bits. Address has
    a forced alignment, so column does not contain alignment bits.
    """
    def __init__(self, colbits, address_align):
        self.colbits       = colbits
        self.address_align = address_align

    def row(self, address):
        split = self.colbits - self.address_align
        return address[split:]

    def col(self, address):
        split = self.colbits - self.address_align
        return Cat(Replicate(0, self.address_align), address[:split])

# BankMachine --------------------------------------------------------------------------------------
      
class BankMachine(Module, AutoCSR):
    """Converts requests from ports into DRAM commands

    BankMachine abstracts single DRAM bank by keeping track of the currently
    selected row. It converts requests from LiteDRAMCrossbar to targetted
    to that bank into DRAM commands that go to the Multiplexer, inserting any
    needed activate/precharge commands (with optional auto-precharge). It also
    keeps track and enforces some DRAM timings (other timings are enforced in
    the Multiplexer).

    BankMachines work independently from the data path (which connects
    LiteDRAMCrossbar with the Multiplexer directly).

    Stream of requests from LiteDRAMCrossbar is being queued, so that reqeust
    can be "looked ahead", and auto-precharge can be performed (if enabled in
    settings).

    Lock (cmd_layout.lock) is used to synchronise with LiteDRAMCrossbar. It is
    being held when:
     - there is a valid command awaiting in `cmd_buffer_lookahead` - this buffer
       becomes ready simply when the next data gets fetched to the `cmd_buffer`
     - there is a valid command in `cmd_buffer` - `cmd_buffer` becomes ready
       when the BankMachine sends wdata_ready/rdata_valid back to the crossbar

    Parameters
    ----------
    n : int
        Bank number
    address_width : int
        LiteDRAMInterface address width
    address_align : int
        Address alignment depending on burst length
    nranks : int
        Number of separate DRAM chips (width of chip select)
    settings : ControllerSettings
        LiteDRAMController settings

    Attributes
    ----------
    req : Record(cmd_layout)
        Stream of requests from LiteDRAMCrossbar
    refresh_req : Signal(), in
        Indicates that refresh needs to be done, connects to Refresher.cmd.valid
    refresh_gnt : Signal(), out
        Indicates that refresh permission has been granted, satisfying timings
    cmd : Endpoint(cmd_request_rw_layout)
        Stream of commands to the Multiplexer
    """
    def __init__(self, n, address_width, address_align, nranks, settings, TMRreq, logger):
        self.req = req = Record(cmd_layout(address_width))
        self.TMRreq = TMRreq = TMRRecord(req)
        self.refresh_req = refresh_req = Signal()
        self.refresh_gnt = refresh_gnt = Signal()

        a  = settings.geom.addressbits
        ba = settings.geom.bankbits + log2_int(nranks)
        self.cmd = cmd = stream.Endpoint(cmd_request_rw_layout(a, ba))
        self.TMRcmd = TMRcmd = TMRRecord(cmd)
        
        self.submodules += TMROutput(cmd.valid, TMRcmd.valid)
        self.submodules += TMROutput(cmd.last, TMRcmd.last)
        self.submodules += TMROutput(cmd.first, TMRcmd.first)
        self.submodules += TMRInput(TMRcmd.ready, cmd.ready)
        self.submodules += TMROutput(cmd.a, TMRcmd.a)
        self.submodules += TMROutput(cmd.ba, TMRcmd.ba)
        self.submodules += TMROutput(cmd.cas, TMRcmd.cas)
        self.submodules += TMROutput(cmd.ras, TMRcmd.ras)
        self.submodules += TMROutput(cmd.we, TMRcmd.we)
        self.submodules += TMROutput(cmd.is_cmd, TMRcmd.is_cmd)
        self.submodules += TMROutput(cmd.is_read, TMRcmd.is_read)
        self.submodules += TMROutput(cmd.is_write, TMRcmd.is_write)

        # # #

        auto_precharge = Signal()
        
        # Logging ----------------------------------------------------------------------------------
        
        log_n = Signal(8)
        log_num = Signal(8)
        log_addr = Signal(32)
        
        log_sigs = []
        log_codes = []
        
        def make_log_sig(code):
            log_sig = Signal()
            log_sigs.append(log_sig)
            log_codes.append(code)
            return log_sig
            
        def log_rising_edge(log_sig, track_sig):
            track_edge = Signal()
            self.sync += track_edge.eq(track_sig)
            self.sync += If(track_sig & ~track_edge, log_sig.eq(1))
            
        def create_rising_edge_log_sig(code, track_sig):
            log_sig = make_log_sig(code)
            log_rising_edge(log_sig, track_sig)
        
        # Verbose messages -----------------------
        
        fsm_state = Signal(max=4)
        if settings.verbose_logging:
            # Control signal logs
            create_rising_edge_log_sig(0, cmd.valid)
            create_rising_edge_log_sig(1, cmd.ready)
            
            # FSM logs
            
            create_rising_edge_log_sig(2, fsm_state == 0)
            create_rising_edge_log_sig(3, fsm_state == 1)
            create_rising_edge_log_sig(4, fsm_state == 2)
            create_rising_edge_log_sig(5, fsm_state == 3)
            create_rising_edge_log_sig(6, fsm_state == 4)
            
            # FIFO signal logs
            create_rising_edge_log_sig(9, req.valid)
            create_rising_edge_log_sig(10, req.wdata_ready | req.rdata_valid)
        
        # Error messages -------------------------
        
        # Non-unanimous voting logs
        log_addr_vote = make_log_sig(7)
        log_valid_vote = make_log_sig(8)
        
        self.comb += log_n.eq(n)
        
        log_message = Cat(log_addr, log_num, log_n)
        
        message, ready, request = logger.get_log_port()
        
        # Connect log message
        self.comb += [message.eq(log_message)]
        
        # Send message based on priority
        statement = None
        for log_sig, log_code in zip(log_sigs, log_codes):
            if statement is not None:
                statement = statement.Elif(log_sig, log_num.eq(log_code))
            else:
                statement = If(log_sig, log_num.eq(log_code))
        self.comb += statement
            
        # Confirm message based on priority
        statement = None
        for log_sig, log_code in zip(log_sigs, log_codes):
            if statement is not None:
                statement = statement.Elif(log_sig, log_sig.eq(0))
            else:
                statement = If(log_sig, log_sig.eq(0))
        self.sync += If(ready, statement)
                
        # Request when waiting for any log message
        self.comb += request.eq(reduce(or_, log_sigs))
        
        # TMR Setup --------------------------------------------------------------------------------
        
        connect_TMR(self, TMRreq, req, master=False)

        # Command buffer TMR -----------------------------------------------------------------------
        
        cmd_buffer_layout    = [("we", 1), ("addr", len(req.addr))]
        
        def make_buffer():
            cmd_buffer_lookahead = stream.SyncFIFO(
                cmd_buffer_layout, settings.cmd_buffer_depth,
                buffered=settings.cmd_buffer_buffered)
                
            cmd_buffer = stream.Buffer(cmd_buffer_layout) # 1 depth buffer to detect row change
            
            self.submodules += cmd_buffer_lookahead, cmd_buffer
            
            self.comb += [
                req.connect(cmd_buffer_lookahead.sink, keep={"valid", "we", "addr"}),
                cmd_buffer_lookahead.source.connect(cmd_buffer.sink),
                cmd_buffer.source.ready.eq(req.wdata_ready | req.rdata_valid),
            ]
            return (cmd_buffer_lookahead, cmd_buffer)
            
        cmd_buffer_lookahead, cmd_buffer = make_buffer()
        cmd_buffer_lookahead2, cmd_buffer2 = make_buffer()
        cmd_buffer_lookahead3, cmd_buffer3 = make_buffer()
        
        
        lockSig = Cat(cmd_buffer_lookahead.source.valid | cmd_buffer.source.valid, 
                        cmd_buffer_lookahead2.source.valid | cmd_buffer2.source.valid, 
                        cmd_buffer_lookahead3.source.valid | cmd_buffer3.source.valid)
        self.submodules += TMRInput(lockSig, req.lock)
        
        #Vote req ready
        readySig = cmd_buffer_lookahead.sink.ready & cmd_buffer_lookahead2.sink.ready & cmd_buffer_lookahead3.sink.ready
        self.comb += req.ready.eq(readySig)
        
        #Vote lookahead addr
        lookAddrSig = Cat(cmd_buffer_lookahead.source.addr, cmd_buffer_lookahead2.source.addr, cmd_buffer_lookahead3.source.addr)
        lookAddrVote = TMRInput(lookAddrSig)
        self.submodules += lookAddrVote
        
        #Vote buffer addr
        self.create_addr_errors = CSRStorage()
        
        self.submodules.addr_error_timer = addr_error_timer = tXXDController(settings.addr_error_time)
        self.comb += addr_error_timer.valid.eq(addr_error_timer.ready)
        
        addr_error = Signal(len(req.addr))
        self.comb += If(self.create_addr_errors.storage & addr_error_timer.ready, addr_error.eq(-1)).Else(addr_error.eq(0))
        #self.comb += If(self.create_addr_errors.w, addr_error.eq(-1)).Else(addr_error.eq(0))
        
        buffer_addr = Signal(len(cmd_buffer.source.addr))
        self.comb += buffer_addr.eq(cmd_buffer.source.addr ^ addr_error)
        
        bufAddrSig = Cat(buffer_addr, cmd_buffer2.source.addr, cmd_buffer3.source.addr)
        bufAddrVote = TMRInput(bufAddrSig)
        self.submodules += bufAddrVote
        
        log_rising_edge(log_addr_vote, ~bufAddrVote.unanimous)
        #log_rising_edge(log_addr_vote, addr_error_timer.ready)
        #log_rising_edge(log_addr_vote, self.create_addr_errors.storage)
        
        self.comb += [log_addr[12:15].eq(n), 
                      log_addr[5:12].eq(bufAddrVote.control[:7]),
                      log_addr[15:].eq(bufAddrVote.control[7:]),
                      log_addr[30].eq(1)]
        
        #Vote lookahead valid
        lookValidSig = Cat(cmd_buffer_lookahead.source.valid, cmd_buffer_lookahead2.source.valid, cmd_buffer_lookahead3.source.valid)
        lookValidVote = TMRInput(lookValidSig)
        self.submodules += lookValidVote
        
        #Vote buffer valid
        bufValidSig = Cat(cmd_buffer.source.valid, cmd_buffer2.source.valid, cmd_buffer3.source.valid)
        bufValidVote = TMRInput(bufValidSig)
        self.submodules += bufValidVote
        
        log_rising_edge(log_valid_vote, ~(cmd_buffer.source.valid == cmd_buffer2.source.valid == cmd_buffer3.source.valid))
        
        #Vote buffer we
        bufWeSig = Cat(cmd_buffer.source.we, cmd_buffer2.source.we, cmd_buffer3.source.we)
        bufWeVote = TMRInput(bufWeSig)
        self.submodules += bufWeVote

        # Row tracking -----------------------------------------------------------------------------
        slicer = _AddressSlicer(settings.geom.colbits, address_align)
        
        row        = Signal(settings.geom.rowbits)
        row_opened = Signal()
        row_hit    = Signal()
        row_open   = Signal()
        row_close  = Signal()
        self.comb += row_hit.eq(row == slicer.row(bufAddrVote.control))
        self.sync += \
            If(row_close,
                row_opened.eq(0)
            ).Elif(row_open,
                row_opened.eq(1),
                row.eq(slicer.row(bufAddrVote.control))
            )

        # Address generation -----------------------------------------------------------------------
        row_col_n_addr_sel = Signal()
        self.comb += [
            cmd.ba.eq(n),
            If(row_col_n_addr_sel,
                cmd.a.eq(slicer.row(bufAddrVote.control))
            ).Else(
                cmd.a.eq((auto_precharge << 10) | slicer.col(bufAddrVote.control)) # Vote addr
            )
        ]

        # tWTP (write-to-precharge) controller -----------------------------------------------------
        write_latency = math.ceil(settings.phy.cwl / settings.phy.nphases)
        precharge_time = write_latency + settings.timing.tWR + settings.timing.tCCD # AL=0
        self.submodules.twtpcon = twtpcon = tXXDController(precharge_time)
        self.comb += twtpcon.valid.eq(cmd.valid & cmd.ready & cmd.is_write)
        
        self.submodules.twtpcon2 = twtpcon2 = tXXDController(precharge_time)
        self.comb += twtpcon2.valid.eq(cmd.valid & cmd.ready & cmd.is_write)
        
        self.submodules.twtpcon3 = twtpcon3 = tXXDController(precharge_time)
        self.comb += twtpcon3.valid.eq(cmd.valid & cmd.ready & cmd.is_write)
        
        twtpSig = Cat(twtpcon.ready, twtpcon2.ready, twtpcon3.ready)
        twtpVote = TMRInput(twtpSig)
        self.submodules += twtpVote

        # tRC (activate-activate) controller -------------------------------------------------------
        self.submodules.trccon = trccon = tXXDController(settings.timing.tRC)
        self.comb += trccon.valid.eq(cmd.valid & cmd.ready & row_open)
        
        self.submodules.trccon2 = trccon2 = tXXDController(settings.timing.tRC)
        self.comb += trccon2.valid.eq(cmd.valid & cmd.ready & row_open)
        
        self.submodules.trccon3 = trccon3 = tXXDController(settings.timing.tRC)
        self.comb += trccon3.valid.eq(cmd.valid & cmd.ready & row_open)
        
        trcSig = Cat(trccon.ready, trccon2.ready, trccon3.ready)
        trcVote = TMRInput(trcSig)
        self.submodules += trcVote

        # tRAS (activate-precharge) controller -----------------------------------------------------
        self.submodules.trascon = trascon = tXXDController(settings.timing.tRAS)
        self.comb += trascon.valid.eq(cmd.valid & cmd.ready & row_open)
        
        self.submodules.trascon2 = trascon2 = tXXDController(settings.timing.tRAS)
        self.comb += trascon2.valid.eq(cmd.valid & cmd.ready & row_open)
        
        self.submodules.trascon3 = trascon3 = tXXDController(settings.timing.tRAS)
        self.comb += trascon3.valid.eq(cmd.valid & cmd.ready & row_open)
        
        trasSig = Cat(trascon.ready, trascon2.ready, trascon3.ready)
        trasVote = TMRInput(trasSig)
        self.submodules += trasVote

        # Auto Precharge generation ----------------------------------------------------------------
        # generate auto precharge when current and next cmds are to different rows
        if settings.with_auto_precharge:
            self.comb += \
                If(lookValidVote.control & bufValidVote.control,
                    If(slicer.row(lookAddrVote.control) !=
                       slicer.row(bufAddrVote.control),
                        auto_precharge.eq(row_close == 0)
                    )
                )

        # Control and command generation FSM -------------------------------------------------------
        # Note: tRRD, tFAW, tCCD, tWTR timings are enforced by the multiplexer
        self.submodules.fsm = fsm = FSM()
        fsm.act("REGULAR",
            fsm_state.eq(0),
            If(refresh_req,
                NextState("REFRESH")
            ).Elif(bufValidVote.control,
                If(row_opened,
                    If(row_hit,
                        cmd.valid.eq(1),
                        If(bufWeVote.control,
                            req.wdata_ready.eq(cmd.ready),
                            cmd.is_write.eq(1),
                            cmd.we.eq(1),
                        ).Else(
                            req.rdata_valid.eq(cmd.ready),
                            cmd.is_read.eq(1)
                        ),
                        cmd.cas.eq(1),
                        If(cmd.ready & auto_precharge,
                           NextState("AUTOPRECHARGE")
                        )
                    ).Else(  # row_opened & ~row_hit
                        NextState("PRECHARGE")
                    )
                ).Else(  # ~row_opened
                    NextState("ACTIVATE")
                )
            )
        )
        fsm.act("PRECHARGE",
            fsm_state.eq(1),
            # Note: we are presenting the column address, A10 is always low
            If(twtpVote.control & trasVote.control,
                cmd.valid.eq(1),
                If(cmd.ready,
                    NextState("TRP")
                ),
                cmd.ras.eq(1),
                cmd.we.eq(1),
                cmd.is_cmd.eq(1)
            ),
            row_close.eq(1)
        )
        fsm.act("AUTOPRECHARGE",
            fsm_state.eq(2),
            If(twtpVote.control & trasVote.control,
                NextState("TRP")
            ),
            row_close.eq(1)
        )
        fsm.act("ACTIVATE",
            fsm_state.eq(3),
            If(trcVote.control,
                row_col_n_addr_sel.eq(1),
                row_open.eq(1),
                cmd.valid.eq(1),
                cmd.is_cmd.eq(1),
                If(cmd.ready,
                    NextState("TRCD")
                ),
                cmd.ras.eq(1)
            )
        )
        fsm.act("REFRESH",
            fsm_state.eq(4),
            If(twtpVote.control,
                refresh_gnt.eq(1),
            ),
            row_close.eq(1),
            cmd.is_cmd.eq(1),
            If(~refresh_req,
                NextState("REGULAR")
            )
        )
        fsm.delayed_enter("TRP", "ACTIVATE", settings.timing.tRP - 1)
        fsm.delayed_enter("TRCD", "REGULAR", settings.timing.tRCD - 1)
