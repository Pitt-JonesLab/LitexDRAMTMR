#
# This file is part of LiteDRAM.
#
# Copyright (c) 2016-2019 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

from migen import *

from litex.soc.interconnect.csr import AutoCSR

from litedram.dfii import DFIInjector
from litedram.core.controller import ControllerSettings, LiteDRAMController
from litedram.core.crossbar import LiteDRAMCrossbar
from litedram.common import *

from litex.soc.interconnect.csr_eventmanager import *

# Core ---------------------------------------------------------------------------------------------

class LiteDRAMCore(Module, AutoCSR):
    def __init__(self, phy, geom_settings, timing_settings, clk_freq, **kwargs):
        self.submodules.dfii = DFIInjector(
            addressbits = max(geom_settings.addressbits, getattr(phy, "addressbits", 0)),
            bankbits    = max(geom_settings.bankbits, getattr(phy, "bankbits", 0)),
            nranks      = phy.settings.nranks,
            databits    = phy.settings.dfi_databits,
            nphases     = phy.settings.nphases)
        self.comb += self.dfii.master.connect(phy.dfi)

        self.submodules.ev = EventManager()
        self.ev.tick = EventSourcePulse()
        self.ev.finalize()

        kwargs['log_interrupt'] = self.ev.tick

        self.submodules.controller = controller = LiteDRAMController(
            phy_settings    = phy.settings,
            geom_settings   = geom_settings,
            timing_settings = timing_settings,
            clk_freq        = clk_freq,
            **kwargs)
        #self.comb += controller.dfi.connect(self.dfii.slave)
        self.comb += controller.TMRdfi.connect(self.dfii.TMRslave)

        self.submodules.crossbar = LiteDRAMCrossbar(controller.interface, controller.TMRinterface)
        
        # Tick interrupt
        #self.submodules.tick_timer = tick_timer = tXXDController(125000000)
        #self.comb += tick_timer.valid.eq(tick_timer.ready)
        #self.comb += self.ev.tick.trigger.eq(tick_timer.ready)
