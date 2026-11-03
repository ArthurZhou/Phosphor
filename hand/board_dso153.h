/* DSO-153 补丁：AT32F435/437 USB OTG FS（Synopsys DWC2）板级与寄存器定义
 *
 * 依据：Artery 官方 AT32F435_437 Firmware Library
 *   CRM 基址        0x40023800   （与固件里逆向出来的时钟寄存器完全一致）
 *   CRM_AHBEN2      0x34  bit7  = otgfs1en   -> USB 口 1 (0x50000000)，IRQ 67
 *   CRM_AHBEN1      0x30  bit29 = otgfs2en   -> USB 口 2 (0x40040000)，IRQ 77
 *   CRM_MISC1       0xA0  bit13 = hick_to_usb
 *
 * 重要更正（v1~v3 的错误，全部来自没对照官方库）：
 *   1) 时钟写到 0x30 bit7 = GPIOHEN（v1/v2），USB 时钟从没开过；
 *      0x14 是 AHBRST2（复位），写它更是错上加错。
 *   2) GCCFG bit16 是 pwrdown，但 AT32 的语义是「1 = 退出掉电」
 *      （官方 usb_global_init 里 pwrdown 置 TRUE，注释写的是 exit power down mode）。
 *      v3 把它清成 0 等于让 PHY 一直掉电，主机连 D+ 上拉都看不到。
 *   3) DCTL bit0 是 rwkupsig（远程唤醒），bit1 才是 sftdiscon（软断开）。
 *   4) DWC2 收 SETUP 依赖 DOEPMSK 的 setupmsk(bit3)，且 EP0 OUT 必须显式武装
 *      （PKTCNT=1 / XFERSIZE=64 / STUPCNT=3），否则永远收不到控制请求。
 */
#ifndef BOARD_DSO153_H
#define BOARD_DSO153_H

#include <stdint.h>

/* 外设基址 */
#define CRM_BASE     0x40023800u

/* 时钟使能寄存器 */
#define CRM_CTRL     (*(volatile uint32_t *)(CRM_BASE + 0x00))   /* bit0 hicken, bit1 hickstbl */
#define CRM_AHBEN1   (*(volatile uint32_t *)(CRM_BASE + 0x30))
#define CRM_AHBEN2   (*(volatile uint32_t *)(CRM_BASE + 0x34))
#define CRM_MISC1    (*(volatile uint32_t *)(CRM_BASE + 0xA0))   /* bit13 hick_to_usb */
#define CRM_MISC2    (*(volatile uint32_t *)(CRM_BASE + 0xA4))   /* [15:12] usbdiv */

/* 两个 OTG 口二选一。默认 OTG1（0x50000000，多数设计的 Type-C 都挂这个口）；
 * 若 OTG1 上去主机毫无反应，再编 USB_USE_OTG2 的版本试 OTG2。 */
#ifdef USB_USE_OTG2
#define USBFS_BASE   0x40040000u
#define USBFS_IRQn   77u
#define OTG_CLK_REG  CRM_AHBEN1
#define OTG_CLK_BIT  29u
#else
#define USBFS_BASE   0x50000000u
#define USBFS_IRQn   67u
#define OTG_CLK_REG  CRM_AHBEN2
#define OTG_CLK_BIT  7u
#endif

/* CRM_MISC1 bit13: 选 HICK 48MHz 作为 USB 时钟源（1 = HICK，0 = PLL）。
 * 默认用 HICK：完全不碰 PLL，不会影响主频和 5MS/s 采样时序。
 * 如果 HICK 精度不够导致枚举不稳，再改回 0 用 PLL。 */
#define USB_CLOCK_FROM_HICK 1

#endif /* BOARD_DSO153_H */
