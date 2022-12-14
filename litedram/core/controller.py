#
# This file is part of LiteDRAM.
#
# Copyright (c) 2015 Sebastien Bourdeauducq <sb@m-labs.hk>
# Copyright (c) 2016-2019 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""LiteDRAM Controller."""

from migen import *

from migen.genlib.fifo import *

from litedram.common import *
from litedram.phy import dfi
from litedram.core.refresher import Refresher
from litedram.core.bankmachine import BankMachine
from litedram.core.multiplexer import Multiplexer
from litedram.core.logging import LoggingSystem

from litex.soc.interconnect.csr import *

# Settings -----------------------------------------------------------------------------------------

class ControllerSettings(Settings):
    def __init__(self,
        # Command buffers
        cmd_buffer_depth    = 8,
        cmd_buffer_buffered = False,

        # Read/Write times
        read_time           = 32,
        write_time          = 16,

        # Bandwidth
        with_bandwidth      = False,

        # Refresh
        with_refresh        = True,
        refresh_cls         = Refresher,
        refresh_zqcs_freq   = 1e0,
        refresh_postponing  = 1,
        
        # TMR
        with_tmr            = True,
        
        # Logging
        logger              = None,
        verbose_logging     = False,
        
        # Error Injection
        addr_error_time     = 125000000,

        # Auto-Precharge
        with_auto_precharge = True,

        # Address mapping
        address_mapping     = "ROW_BANK_COL"):
        self.set_attributes(locals())

# Controller ---------------------------------------------------------------------------------------

class LiteDRAMController(Module, AutoCSR):
    def __init__(self, phy_settings, geom_settings, timing_settings, clk_freq,
        controller_settings=ControllerSettings(), **kwargs):
        if phy_settings.memtype == "SDR":
            burst_length = phy_settings.nphases
        else:
            burst_length = burst_lengths[phy_settings.memtype]
        address_align = log2_int(burst_length)

        # Settings ---------------------------------------------------------------------------------
        self.settings        = controller_settings
        self.settings.phy    = phy_settings
        self.settings.geom   = geom_settings
        self.settings.timing = timing_settings

        nranks = phy_settings.nranks
        nbanks = 2**geom_settings.bankbits

        # LiteDRAM Interface (User) ----------------------------------------------------------------
        self.interface = interface = LiteDRAMInterface(address_align, self.settings)

        self.TMRinterface = TMRinterface = TMRRecord(interface)

        # DFI Interface (Memory) -------------------------------------------------------------------
        self.dfi = dfi.Interface(
            addressbits = geom_settings.addressbits,
            bankbits    = geom_settings.bankbits,
            nranks      = phy_settings.nranks,
            databits    = phy_settings.dfi_databits,
            nphases     = phy_settings.nphases)
            
        self.TMRdfi = TMRRecord(self.dfi)
        
        connect_TMR(self, self.TMRdfi, self.dfi)
        
        # Logging Buffer CSR -----------------------------------------------------------------------
        
        self.submodules.logger = logger = LoggingSystem()
        self.settings.logger = logger
        
        self.log_interrupt = kwargs['log_interrupt']
        self.comb += self.log_interrupt.trigger.eq(logger.readable)

        # Refresher --------------------------------------------------------------------------------
        self.submodules.refresher = self.settings.refresh_cls(self.settings,
            clk_freq   = clk_freq,
            zqcs_freq  = self.settings.refresh_zqcs_freq,
            postponing = self.settings.refresh_postponing)

        # Bank Machines ----------------------------------------------------------------------------
        bank_machines = []
        for n in range(nranks*nbanks):
            bank_machine = BankMachine(n,
                address_width = interface.address_width,
                address_align = address_align,
                nranks        = nranks,
                settings      = self.settings,
                TMRreq        = getattr(TMRinterface, "bank"+str(n)),
                logger        = logger)
            bank_machines.append(bank_machine)
            self.submodules += bank_machine
            setattr(self, 'bm_'+str(n), bank_machine)
            #self.comb += getattr(interface, "bank"+str(n)).connect(bank_machine.req)
            self.comb += getattr(TMRinterface, "bank"+str(n)).connect(bank_machine.TMRreq)

        # Multiplexer ------------------------------------------------------------------------------
        self.submodules.multiplexer = Multiplexer(
            settings      = self.settings,
            bank_machines = bank_machines,
            refresher     = self.refresher,
            dfi           = self.dfi,
            interface     = interface,
            TMRinterface  = TMRinterface,
            logger        = logger)

    #def get_csrs(self):
    #    return self.multiplexer.get_csrs()
