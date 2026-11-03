/**
  **************************************************************************
  * @file     at32f435_437_conf.h
  * @brief    DSO-153 补丁版外设配置头
  *
  * 由官方 vcp_loopback 例程的 inc/at32f435_437_conf.h 裁剪而来：
  * 原文件把 30 多个外设模块全打开，补丁只需要跑 USB，其余模块的头文件
  * 我们根本没抓，留着就是一堆 include 错误。
  * 保留的：CRM（时钟）、GPIO（PA11/PA12 复用）、USB（OTG 寄存器）、
  *         MISC（nvic_irq_enable）、ACC（HICK 时钟校准，备用）。
  **************************************************************************
  */
#ifndef __AT32F435_437_CONF_H
#define __AT32F435_437_CONF_H

#ifdef __cplusplus
extern "C" {
#endif

/* HEXT 值：官方默认 8MHz。DSO-153 板上晶振若不同，编译时 -DHEXT_VALUE=... 覆盖 */
#if !defined  HEXT_VALUE
#define HEXT_VALUE                       ((uint32_t)8000000)
#endif

#define HEXT_STARTUP_TIMEOUT             ((uint16_t)0x3000)
#define HICK_VALUE                       ((uint32_t)8000000)
#define LEXT_VALUE                       ((uint32_t)32768)

/* 只开补丁真正用到的模块 */
#define CRM_MODULE_ENABLED
#define GPIO_MODULE_ENABLED
#define USB_MODULE_ENABLED
#define MISC_MODULE_ENABLED
#define ACC_MODULE_ENABLED

#ifdef CRM_MODULE_ENABLED
#include "at32f435_437_crm.h"
#endif
#ifdef GPIO_MODULE_ENABLED
#include "at32f435_437_gpio.h"
#endif
#ifdef ACC_MODULE_ENABLED
#include "at32f435_437_acc.h"
#endif
#ifdef MISC_MODULE_ENABLED
#include "at32f435_437_misc.h"
#endif
#ifdef USB_MODULE_ENABLED
#include "at32f435_437_usb.h"
#endif

#ifdef __cplusplus
}
#endif

#endif
