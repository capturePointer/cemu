import os

import unicorn
import keystone
import capstone

from .arch import Architecture
from .utils import get_arch_mode, assemble


class Emulator:

    def __init__(self, mode, *args, **kwargs):
        self.mode = mode
        self.use_step_mode = False
        self.widget = None
        self.reinit()
        return


    def reinit(self):
        self.vm = None
        self.code = None
        self.is_running = False
        self.stop_now = False
        self.num_insns = -1
        self.areas = {}
        self.registers = {}
        self.create_new_vm()
        return


    def pprint(self, x):
        if self.widget is None:
            print(x)
        else:
            self.widget.emuWidget.editor.append(x)
        return


    def log(self, x):
        if self.widget is None:
            print(x)
        else:
            self.widget.logWidget.editor.append(x)
        return


    def unicorn_register(self, reg):
        if self.mode in (Architecture.X86_16_INTEL, Architecture.X86_16_ATT,
                         Architecture.X86_32_INTEL, Architecture.X86_32_ATT,
                         Architecture.X86_64_INTEL, Architecture.X86_64_ATT):
            return getattr(unicorn.x86_const, "UC_X86_REG_%s"%reg.upper())

        if self.mode in (Architecture.ARM_LE, Architecture.ARM_BE,
                         Architecture.ARM_THUMB_LE, Architecture.ARM_THUMB_BE):
            return getattr(unicorn.arm_const, "UC_ARM_REG_%s"%reg.upper())

        if self.mode==Architecture.ARM_AARCH64:
            return getattr(unicorn.arm64_const, "UC_ARM64_REG_%s"%reg.upper())

        if self.mode in (Architecture.PPC, Architecture.PPC64):
            return getattr(unicorn.ppc_const, "UC_PPC_REG_%s" % reg.upper())

        if self.mode in (Architecture.MIPS, Architecture.MIPS_BE,
                         Architecture.MIPS64, Architecture.MIPS64_BE):
            return getattr(unicorn.mips_const, "UC_MIPS_REG_%s" % reg.upper())

        if self.mode in (Architecture.SPARC, Architecture.SPARC_BE, Architecture.SPARC64):
            return getattr(unicorn.sparc_const, "UC_SPARC_REG_%s" %reg.upper())

        raise Exception("Cannot find register '%s' for arch '%s'" % (reg, self.mode))


    def get_register_value(self, r):
        ur = self.unicorn_register(r)
        return self.vm.reg_read(ur)


    def unicorn_permissions(self, perms):
        p = 0
        for perm in perms.split("|"):
            p |= getattr(unicorn, "UC_PROT_%s" % perm.upper())
        return p


    def create_new_vm(self):
        arch, mode, endian = get_arch_mode("unicorn", self.mode)
        self.vm = unicorn.Uc(arch, mode | endian)
        self.vm.hook_add(unicorn.UC_HOOK_BLOCK, self.hook_block)
        self.vm.hook_add(unicorn.UC_HOOK_CODE, self.hook_code)
        self.vm.hook_add(unicorn.UC_HOOK_INTR, self.hook_interrupt)
        self.vm.hook_add(unicorn.UC_HOOK_MEM_WRITE, self.hook_mem_access)
        self.vm.hook_add(unicorn.UC_HOOK_MEM_READ, self.hook_mem_access)
        return


    def populate_memory(self, areas):
        for name, address, size, permission, input_file in areas:
            perm = self.unicorn_permissions(permission)
            self.vm.mem_map(address, size, perm)
            self.areas[name] = [address, size, permission,]

            msg = ">>> map %s @%x (size=%d,perm=%s)" % (name, address, size, permission)
            if input_file is not None and os.access(input_file, os.R_OK):
                code = open(input_file, 'rb').read()
                self.vm.mem_write(address, bytes(code[:size]))
                msg += " and content from '%s'" % input_file

            self.log(msg)

        self.start_addr = self.areas[".text"][0]
        self.end_addr = -1
        return True


    def populate_registers(self, registers):
        for r in registers.keys():
            ur = self.unicorn_register(r)
            self.vm.reg_write(ur, registers[r])
            self.log(">>> register %s = %x" % (r, registers[r]))

        # fix $PC
        ur = self.unicorn_register(self.mode.get_pc())
        self.vm.reg_write(ur, self.areas[".text"][0])

        # fix $SP
        ur = self.unicorn_register(self.mode.get_sp())
        self.vm.reg_write(ur, self.areas[".stack"][0])
        return True


    def compile_code(self, code, update_end_addr=True):
        code = b" ; ".join(code)
        self.log(">>> Assembly using keystone for '%s': %s" % (self.mode.get_title(), code))
        self.code, self.num_insns = assemble(code, self.mode)
        if self.num_insns < 0:
            self.log(">>> Failed to compile code")
            return False

        self.log(">>> %d instructions compiled" % self.num_insns)

        # update end_addr since we know the size of the code to execute
        if update_end_addr:
            self.end_addr = self.start_addr + len(self.code)
        return True


    def map_code(self):
        if ".text" not in self.areas.keys():
            self.log("Missing text area (add a .text section in the Mapping tab)")
            return False

        if self.code is None:
            self.log("No code defined yet")
            return False

        addr = self.areas[".text"][0]
        self.log(">>> mapping .text at %#x" % addr)
        self.vm.mem_write(addr, bytes(self.code))
        return True


    def disassemble_one_instruction(self, code, addr):
        arch, mode, endian = get_arch_mode("capstone", self.mode)
        cs = capstone.Cs(arch, mode | endian)
        if self.mode in (Architecture.X86_16_ATT, Architecture.X86_32_ATT, Architecture.X86_64_ATT):
            cs.syntax = capstone.CS_OPT_SYNTAX_ATT
        for i in cs.disasm(bytes(code), addr):
            return i


    def hook_code(self, emu, address, size, user_data):
        code = self.vm.mem_read(address, size)
        insn = self.disassemble_one_instruction(code, address)

        if self.stop_now:
            self.start_addr = self.get_register_value(self.mode.get_pc())
            emu.emu_stop()
            return

        self.log(">> Executing instruction at 0x{:x}".format(address))
        self.pprint(">>> 0x{:x}: {:s} {:s}".format(insn.address, insn.mnemonic, insn.op_str))

        if self.use_step_mode:
            self.stop_now = True
        return


    def hook_block(self, emu, addr, size, misc):
        self.pprint(">>> Entering new block at 0x{:x}".format(addr))
        return


    def hook_interrupt(self, emu, intno, data):
        self.pprint(">>> Triggering interrupt #{:d}".format(intno))
        return


    def hook_mem_access(self, emu, access, address, size, value, user_data):
        if access == unicorn.UC_MEM_WRITE:
            self.pprint(">>> MEM_WRITE : *%#x = %#x (size = %u)"% (address, value, size))
        elif access == unicorn.UC_MEM_READ:
            self.pprint(">>> MEM_READ : reg = *%#x (size = %u)" % (address, size))
        return


    def run(self):
        try:
            self.vm.emu_start(self.start_addr, self.end_addr)
        except unicorn.unicorn.UcError as e:
            self.vm.emu_stop()
            self.log("An error occured during emulation")
            return

        if self.get_register_value( self.mode.get_pc() )==self.end_addr:
            self.pprint(">>> End of emulation")
            self.widget.commandWidget.runButton.setDisabled(True)
            self.widget.commandWidget.stepButton.setDisabled(True)
        return


    def stop(self):
        for area in self.areas.keys():
            addr, size = self.areas[area][0:2]
            self.vm.mem_unmap(addr, size)

        del self.vm
        self.vm = None
        self.is_running = False
        return


    def lookup_map(self, mapname):
        for area in self.areas.keys():
            if area == mapname:
                return self.areas[area][0]
        return None
