/* DSO-153 手写寄存器版补丁 —— 独立链接脚本
 * 与 tools/stub/link.ld 完全分开，避免和官方库那条线互相覆盖。
 *
 * 代码放 APP 镜像后的空闲 flash（0x08035B00 起）。
 * .bss 从 0x2000A580 起，只占 640 字节（到 0x2000A7FF），
 * 栈底由 patch.py 抬到 0x2000A800，栈仍有 3456 字节。
 */
ENTRY(hook)

orig_band_continue = 0x08008E68;
orig_task          = 0x08008EE0;

MEMORY
{
  PATCH_FLASH (rx) : ORIGIN = 0x08035B00, LENGTH = 0x00050000
  PATCH_RAM   (rw) : ORIGIN = 0x2000A580, LENGTH = 0x00000280
}

SECTIONS
{
  .text :
  {
    KEEP(*(.text.hook))
    KEEP(*(.text.band))
    KEEP(*(.text.usb_irq))
    *(.text)
    *(.text.*)
  } > PATCH_FLASH

  .rodata :
  {
    *(.rodata)
    *(.rodata.*)
    *(.glue_7) *(.glue_7t)
  } > PATCH_FLASH

  .bss (NOLOAD) :
  {
    __bss_start = .;
    *(.bss)
    *(.bss.*)
    *(COMMON)
    . = ALIGN(4);
    __bss_end = .;
  } > PATCH_RAM

  /DISCARD/ : { *(.ARM.exidx*) *(.ARM.extab*) *(.comment) *(.note*) *(.data*) }
}
