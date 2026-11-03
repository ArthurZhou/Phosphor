/* DSO-153 补丁：USB CDC 描述符
 *
 * 设备：CDC-ACM 虚拟串口（Win10+ 免驱，Linux/macOS 直接 /dev/ttyACM*）
 * VID/PID 用 ST 的 CDC 示例值 0x0483:0x5740，系统自带 usbser 驱动能认
 *
 * 布局遵循 CDC 规范（也照顾老 Windows）：
 *   配置 -> 接口0(控制, 1个中断IN) -> Header -> CallMgmt -> ACM -> Union
 *        -> EP3 IN(中断通知) -> 接口1(数据, 2个批量端点) -> EP1 IN / EP2 OUT
 *
 * 9+9+5+5+4+5+7 + 9+7+7 = 67
 */
#ifndef USB_DESC_H
#define USB_DESC_H

#include <stdint.h>

#define USB_VID 0x0483
#define USB_PID 0x5740

#define EP0_SIZE 64
#define EP1_SIZE 64          /* bulk IN  数据上行 */
#define EP2_SIZE 64          /* bulk OUT 命令下行 */
#define EP3_SIZE 8           /* int IN   通知（实际不用，占位求兼容） */

#define CDC_EP_IN    0x81
#define CDC_EP_OUT   0x02
#define CDC_EP_NOTIF 0x83

#define USB_CONFIG_TOTAL 67

static const uint8_t usb_dev_desc[18] = {
    18, 0x01,               /* bLength, DEVICE */
    0x00, 0x02,             /* bcdUSB 2.00 */
    0x02, 0x00, 0x00,       /* class=CDC（接口级定义） */
    EP0_SIZE,
    USB_VID & 0xFF, USB_VID >> 8,
    USB_PID & 0xFF, USB_PID >> 8,
    0x00, 0x01,             /* bcdDevice 1.00 */
    1, 2, 3,                /* iManufacturer, iProduct, iSerial */
    1                       /* bNumConfigurations */
};

static const uint8_t usb_cfg_desc[USB_CONFIG_TOTAL] = {
    /* --- 配置 --- */
    9, 0x02,
    USB_CONFIG_TOTAL & 0xFF, USB_CONFIG_TOTAL >> 8,
    2,                                /* bNumInterfaces */
    1,                                /* bConfigurationValue */
    0,                                /* iConfiguration */
    0x80,                             /* bus powered */
    0x32,                             /* 100mA */

    /* --- 接口 0：CDC 控制，1 个端点 --- */
    9, 0x04, 0, 0, 1,
    0x02, 0x02, 0x01,                 /* CDC / ACM / AT commands */
    0,

    /* --- CDC 功能描述符 --- */
    5, 0x24, 0x00, 0x10, 0x01,        /* HEADER, bcdCDC 1.10 */
    5, 0x24, 0x01, 0x00, 0x01,        /* CALL MANAGEMENT */
    4, 0x24, 0x02, 0x02,              /* ACM */
    5, 0x24, 0x06, 0, 1,              /* UNION: 0 -> 1 */

    /* --- 端点 3 IN：中断通知 --- */
    7, 0x05, CDC_EP_NOTIF, 0x03,
    EP3_SIZE, 0x00,
    0x10,                             /* bInterval 16ms */

    /* --- 接口 1：CDC 数据，2 个端点 --- */
    9, 0x04, 1, 0, 2,
    0x0A, 0x00, 0x00,
    0,

    /* --- 端点 1 IN：数据上行 --- */
    7, 0x05, CDC_EP_IN, 0x02,
    EP1_SIZE & 0xFF, EP1_SIZE >> 8,
    0x00,

    /* --- 端点 2 OUT：命令下行 --- */
    7, 0x05, CDC_EP_OUT, 0x02,
    EP2_SIZE & 0xFF, EP2_SIZE >> 8,
    0x00
};

typedef char usb_cfg_size_check[
    (sizeof(usb_cfg_desc) == USB_CONFIG_TOTAL) ? 1 : -1];

#endif /* USB_DESC_H */
