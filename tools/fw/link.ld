/*
 * Phosphor 补丁链接脚本
 *
 * 代码位于 APP 镜像（0x08007000..0x08035AFF）之后的空闲 Flash，起始地址
 * 0x08035B00，可用至 0x0803FFFF（AT32F435CCT7，256KB Flash）。
 *
 * .data 初值随 .rodata 存放在 Flash 中（LMA），运行地址与 .bss 一同位于
 * 0x2000B800 起。该区域位于原厂初始 SP（0x2000B580）之上，不属于原厂链接器
 * 声明的 RAM 范围，也不属于栈，因此原厂栈保持完整的 4096 字节
 * （0x2000A580 - 0x2000B580）。
 *
 * 此前补丁把变量区放在 0x2000A580 并抬升 stack_base 到 0x2000AC20，栈只剩
 * 2400 字节；AUTO 键的调用链最深，溢出后直接破坏补丁自己的 .bss（USB 库的
 * otg_core_struct），表现为“连着 USB 口按 AUTO 第二次死机”。R2 起不再借栈。
 *
 * 补丁不含启动代码：.data 搬运与 .bss 清零由 phosphor_usb.c 中的
 * phosphor_cold_start() 在首次调用 usb_poll() 时完成。
 */
ENTRY(hook)

MEMORY
{
  PATCH_FLASH (rx) : ORIGIN = 0x08035B00, LENGTH = 0x0000A500   /* 至 0x0803FFFF */
  PATCH_RAM   (rw) : ORIGIN = 0x2000B800, LENGTH = 0x00000800   /* 至 0x2000BFFF */
}

SECTIONS
{
  /* push_band 函数体 0x08008E64 + 4：img_hook.S 的 b.w 落点。
   * 必须由链接器解析（汇编期地址未知，直接写立即数会报越界）。 */
  push_body = 0x08008E68;

  .text :
  {
    /* 三个入口仅由向量表从外部跳入，没有 C 语言引用，
     * 需显式保留，否则 --gc-sections 会将其删除 */
    KEEP(*(.text.hook))
    KEEP(*(.text.hook_about))
    KEEP(*(.text.img_hook))
    KEEP(*(.text.usb_irq))
    KEEP(*(.text.fault))
    *(.text)
    *(.text.*)
  } > PATCH_FLASH

  .rodata :
  {
    *(.rodata)
    *(.rodata.*)
    *(.glue_7) *(.glue_7t) *(.vfp11_veneer) *(.v4_bx)
  } > PATCH_FLASH

  . = ALIGN(4);
  __data_lma = .;

  .data : AT(__data_lma)
  {
    __data_start = .;
    *(.data)
    *(.data.*)
    . = ALIGN(4);
    __data_end = .;
  } > PATCH_RAM

  .bss (NOLOAD) :
  {
    __bss_start = .;
    *(.bss)
    *(.bss.*)
    *(COMMON)
    . = ALIGN(4);
    __bss_end = .;
  } > PATCH_RAM

  /* HardFault 现场。放在 .bss 之外并固定地址：冷启动只清 .bss，这里的记录能
   * 跨软件复位保留；故障入口是纯汇编（不用字面量池），靠固定地址直接寻址。 */
  .fault_rec (NOLOAD) :
  {
    . = 0x2000BF00;
    __fault_rec = .;
    . = . + 0x100;
    __fault_rec_end = .;
  } > PATCH_RAM

  /DISCARD/ : { *(.ARM.exidx*) *(.ARM.extab*) *(.comment) *(.note*) }
}
