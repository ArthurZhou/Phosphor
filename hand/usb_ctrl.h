/* USB 控制传输的"纯逻辑"部分
 *
 * 刻意不碰任何寄存器：这样可以拿到 PC 上编译、用一个模拟主机把枚举流程
 * 从头跑一遍（tools/host/test_usb_ctrl.c）。硬件相关的部分在 usb_dwc2.c。
 */
#ifndef USB_CTRL_H
#define USB_CTRL_H

#include <stdint.h>

typedef struct {
    uint8_t bmRequestType;
    uint8_t bRequest;
    uint16_t wValue;
    uint16_t wIndex;
    uint16_t wLength;
} usb_setup_t;

typedef struct {
    const uint8_t *data;   /* IN：要返回的数据 */
    uint16_t len;          /* 实际长度（已按 wLength 截断） */
    uint8_t stall;         /* 1 = 回复 STALL */
    uint8_t out_data;      /* 1 = 主机要发数据下来（DATA OUT 阶段） */
} usb_ctrl_result_t;

#define USB_DESC_DEVICE   1
#define USB_DESC_CONFIG   2
#define USB_DESC_STRING   3
#define USB_DESC_DEV_QUAL 6

/* 控制请求的结果统计，便于调试（可选） */
extern volatile uint32_t usb_ctrl_count;

void    usb_ctrl_init(void);
void    usb_ctrl_handle(const usb_setup_t *s, usb_ctrl_result_t *r);
uint8_t usb_ctrl_config(void);
void    usb_ctrl_set_config(uint8_t cfg);
void    usb_ctrl_set_address(uint8_t addr);
uint8_t usb_ctrl_address(void);

/* CDC 下行数据（主机 -> 设备），driver 收到后塞进这里 */
void    usb_ctrl_line_coding_write(const uint8_t *p);

void    usb_cdc_rx_push(const uint8_t *p, uint16_t n);
int     usb_cdc_rx_pop(uint8_t *out, int max);

#endif /* USB_CTRL_H */
