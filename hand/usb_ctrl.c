/* USB CDC 控制请求处理（不碰寄存器，可在 PC 上单测） */
#include "usb_ctrl.h"
#include "usb_desc.h"

#define REQ_GET_STATUS        0x00
#define REQ_CLEAR_FEATURE     0x01
#define REQ_SET_FEATURE       0x03
#define REQ_SET_ADDRESS       0x05
#define REQ_GET_DESCRIPTOR    0x06
#define REQ_GET_CONFIG        0x08
#define REQ_SET_CONFIG        0x09
#define REQ_GET_INTERFACE     0x0A
#define REQ_SET_INTERFACE     0x0B

#define CDC_SET_LINE_CODING        0x20
#define CDC_GET_LINE_CODING        0x21
#define CDC_SET_CONTROL_LINE_STATE 0x22

volatile uint32_t usb_ctrl_count;

static uint8_t s_config;
static uint8_t s_address;
static uint8_t s_line_coding[7];

/* 字符串描述符：0=语言, 1=厂商, 2=产品, 3=序列号
 * 运行时按 UTF-16LE 拼进静态缓冲，避免手写字节出错 */
static const uint8_t str0[] = {4, 3, 0x09, 0x04};
static const char *const str_tbl[4] = {0, "OpenDSO153", "DSO-153 CDC", "0001"};
static uint8_t str_buf[64];

static uint16_t build_string(int idx)
{
    const char *s = str_tbl[idx];
    uint16_t n = 0;
    while (s[n] && n < 30) n++;
    str_buf[0] = (uint8_t)(2 + 2 * n);
    str_buf[1] = 3;
    for (uint16_t i = 0; i < n; i++) {
        str_buf[2 + 2 * i] = (uint8_t)s[i];
        str_buf[3 + 2 * i] = 0;
    }
    return (uint16_t)(2 + 2 * n);
}

static void put_le16(uint8_t *p, uint16_t v)
{
    p[0] = (uint8_t)v;
    p[1] = (uint8_t)(v >> 8);
}

void usb_ctrl_init(void)
{
    s_config = 0;
    s_address = 0;
    s_line_coding[0] = 0x00; s_line_coding[1] = 0xC2; s_line_coding[2] = 0x01; /* 115200 */
    s_line_coding[3] = 0x00;                                               /* 1 stop */
    s_line_coding[4] = 0x00;                                               /* no parity */
    s_line_coding[5] = 0x08;                                               /* 8 bits */
    usb_ctrl_count = 0;
}

uint8_t usb_ctrl_config(void) { return s_config; }
void usb_ctrl_set_config(uint8_t cfg) { s_config = cfg; }
void usb_ctrl_set_address(uint8_t addr) { s_address = addr; }
uint8_t usb_ctrl_address(void) { return s_address; }

void usb_ctrl_handle(const usb_setup_t *s, usb_ctrl_result_t *r)
{
    const uint8_t *p = 0;
    uint16_t n = 0;

    usb_ctrl_count++;
    r->data = 0;
    r->len = 0;
    r->stall = 0;
    r->out_data = 0;

    switch (s->bRequest) {

    case REQ_GET_DESCRIPTOR:
        switch (s->wValue >> 8) {
        case USB_DESC_DEVICE:
            p = usb_dev_desc;
            n = sizeof(usb_dev_desc);
            break;
        case USB_DESC_CONFIG:
            p = usb_cfg_desc;
            n = sizeof(usb_cfg_desc);
            break;
        case USB_DESC_STRING: {
            uint8_t idx = (uint8_t)(s->wValue & 0xFF);
            if (idx > 3) { r->stall = 1; return; }
            p = idx == 0 ? str0 : str_buf;
            n = idx == 0 ? str0[0] : build_string(idx);
            break;
        }
        default:
            r->stall = 1;   /* 全速设备不需要 DEVICE_QUALIFIER */
            return;
        }
        if (n > s->wLength) n = s->wLength;
        r->data = p;
        r->len = n;
        return;

    case REQ_SET_CONFIG:
        s_config = (uint8_t)(s->wValue & 0xFF);
        return;                     /* 纯状态阶段 */

    case REQ_GET_CONFIG: {
        static uint8_t tmp[1];
        tmp[0] = s_config;
        r->data = tmp;
        r->len = 1;
        return;
    }

    case REQ_SET_ADDRESS:
        s_address = (uint8_t)(s->wValue & 0x7F);
        return;

    case REQ_GET_STATUS: {
        static uint8_t st[2] = {0, 0};
        r->data = st;
        r->len = 2;
        return;
    }

    case REQ_GET_INTERFACE: {
        static uint8_t z[1] = {0};
        r->data = z;
        r->len = 1;
        return;
    }

    case REQ_SET_INTERFACE:
    case REQ_CLEAR_FEATURE:
    case REQ_SET_FEATURE:
        return;

    case CDC_SET_LINE_CODING:
        r->out_data = 1;            /* 主机要发 7 字节下来，由 driver 填 s_line_coding */
        return;

    case CDC_GET_LINE_CODING:
        r->data = s_line_coding;
        r->len = 7;
        return;

    case CDC_SET_CONTROL_LINE_STATE:
        return;

    default:
        r->stall = 1;
        return;
    }
}

/* SET_LINE_CODING 的数据由 driver 收到后写回这里 */
void usb_ctrl_line_coding_write(const uint8_t *p)
{
    for (int i = 0; i < 7; i++) s_line_coding[i] = p[i];
}

/* ---------------- CDC 下行缓冲（主机 -> 设备） ---------------- */
static uint8_t  rx_buf[128];
static uint16_t rx_head, rx_tail;

void usb_cdc_rx_push(const uint8_t *p, uint16_t n)
{
    for (uint16_t i = 0; i < n; i++) {
        uint16_t nx = (uint16_t)((rx_head + 1) & 127);
        if (nx == rx_tail) break;   /* 满 */
        rx_buf[rx_head] = p[i];
        rx_head = nx;
    }
}

int usb_cdc_rx_pop(uint8_t *out, int max)
{
    int k = 0;
    while (k < max && rx_tail != rx_head) {
        out[k++] = rx_buf[rx_tail];
        rx_tail = (uint16_t)((rx_tail + 1) & 127);
    }
    return k;
}
