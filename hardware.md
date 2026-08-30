# SO-ARM101 하드웨어 및 USB 연결 기록

이 문서는 SO-ARM101 Leader/Follower arm과 USB 카메라의 실제 연결 상태, 안정적인 장치 식별자, 확인된 제약을 기록한다. 장치 번호(`/dev/video0`, `/dev/ttyACM0` 등)는 재부팅이나 재연결 후 바뀔 수 있으므로 설정에는 가능한 한 `by-id` 또는 `by-path` 경로를 사용한다.

마지막 실측: 2026-08-30 11:18 UTC
호스트: Ubuntu 22.04.5 LTS, Linux 5.15.0-186-generic, x86_64

## 현재 물리/논리 구성

```text
PC (Intel 00:14.0 xHCI)
├── USB2 root hub, Bus 001, 480 Mbps
│   ├── Root port 2
│   │   └── VIA Labs USB2.0 Hub 2109:2817, 4 ports
│   │       ├── Hub port 1: Bus Servo Adapter → Leader arm
│   │       └── Hub port 2: Bus Servo Adapter → Follower arm
│   ├── Root port 7: Xitech IMX335 USB Camera
│   └── Root port 8: Xitech IMX335 USB Camera
└── USB3 root hub, Bus 002, 10 Gbps
    └── VIA Labs USB3.0 Hub 2109:0817, 5 Gbps companion

PC (NVIDIA 01:00.2 xHCI)
├── USB2 root hub, Bus 003, 480 Mbps (현재 비어 있음)
└── USB3 root hub, Bus 004, 10 Gbps (현재 비어 있음)
```

Waveshare hub는 VIA Labs의 USB2/USB3 companion hub 두 개로 정상 인식된다. OS는 케이스에 인쇄된 `IN1`/`IN2` 선택 위치를 알 수 없지만, hub와 두 arm이 PC 아래에 열거되므로 현재 선택된 upstream이 PC에 연결된 것은 확인된다.

중요: 현재 두 카메라는 Waveshare hub 아래가 아니라 PC의 USB root port 7과 8에 직접 연결되어 있다. 이 구성이 두 카메라 동시 송출에 정상 동작한다.

## Arm Bus Servo Adapter

두 adapter의 공통 정보:

- USB VID:PID: `1a86:55d3`
- Product: `USB Single Serial`
- Linux driver: `cdc_acm`
- 권한: 현재 `deploy` 계정이 `dialout` 그룹이므로 읽기/쓰기 가능
- 현재 점유 프로세스: 없음
- 웹 서비스: `sg dialout` wrapper로 실행되며 2026-08-30 12:57 UTC read-only doctor 통과
- udev: `99-soarm101.rules` 설치 완료, 두 adapter에 `ID_MM_DEVICE_IGNORE=1` 적용

### Servo bus 통신 상태

#### 외부 전원 연결 후

2026-08-30 11:18 UTC에 외부 전원을 연결한 뒤 쓰기 없이 다시 진단했다. 양쪽 arm의
ID 1–6이 모두 STS3215 model number `777`로 응답했다. 모든 모터의 torque 상태는 `0`
(disabled)이었으며 진단 중 torque, position, calibration 레지스터를 변경하지 않았다.

| 항목 | Leader | Follower |
|---|---|---|
| ID | 1–6 모두 응답 | 1–6 모두 응답 |
| Firmware | 3.9 (6개 동일) | 3.10 (6개 동일) |
| 전압 raw | 121–122 | 121–123 |
| 해석 전압 | 약 12.1–12.2V | 약 12.1–12.3V |
| Torque_Enable | 0 (6개 모두) | 0 (6개 모두) |

현재 serial bus와 전체 daisy chain은 정상이다. Leader와 Follower 사이 firmware가 다른 것은
각 arm 내부에서는 일치하므로 즉시 오류로 판단하지 않는다.

#### 외부 전원 연결 전 진단 기록

2026-08-30에 LeRobot 0.6.1/Feetech SDK로 쓰기 없이 진단했다. 두 adapter 모두 serial
port open에는 성공했지만, 기본 `1,000,000 baud` broadcast/direct ping과 LeRobot이
지원하는 전체 baud의 ID 1–6 스캔에서 status packet을 하나도 받지 못했다.

이후 Waveshare가 배포한 `STServo_Python` SDK의 `sms_sts.ping()`으로도 두 포트의
ID 1–6을 재검사했으며 모두 `There is no status packet`으로 동일했다. 특정 LeRobot
버전이나 래퍼의 문제일 가능성은 낮다.

```text
Leader:   port open OK, discovered motors: none
Follower: port open OK, discovered motors: none
```

사용자 확인에 따르면 모터 ID 1–6 설정은 이미 완료된 상태다. 따라서 현재 전체 체인을
분해하여 `lerobot-setup-motors`를 다시 실행하지 않는다.

보드의 빨간 LED는 회로도상 USB `VDDUSB` 측 표시이므로 USB-to-serial 로직 전원이
들어왔다는 뜻이다. 외부 DC 입력이나 servo의 V/G 전원 공급을 증명하지 않는다. 모터 본체에는
별도 상태 LED가 없는 현재 키트 구성이므로 모터별 LED 진단은 적용하지 않는다.

따라서 USB adapter 인식과 servo chain 통신은 별개의 상태다. 현재 calibration을 시작하지
않으며 다음을 현장에서 확인한다.

1. PC USB 제어이므로 두 Bus Servo Adapter (A)의 control-mode jumper가 모두 `B` 위치인지
   확인한다. `A`는 별도 UART 제어용이다.
2. USB-C와 별도로 두 보드의 DC5521 입력에 servo 규격과 일치하는 외부 전원이 연결됐는지
   확인한다. Waveshare SO-ARM101 안내 기준은 `12V 5A`이며 실제 PSU 라벨을 우선한다.
3. 전원을 끈 상태에서 adapter의 bus servo 포트와 1번 servo 사이 3-pin connector가
   `D/V/G` 방향에 맞고 완전히 체결됐는지 확인한다.
4. 위 세 항목이 맞으면 멀티미터로 adapter servo 포트의 V-G 전압을 확인하거나, 전원을 끈 뒤
   1번 servo 하나만 adapter에 연결하여 read-only ping으로 adapter/chain 구간을 분리 진단한다.

| 역할 | Hub 경로 | USB serial | 현재 노드 | 설정에 사용할 안정 경로 |
|---|---|---|---|---|
| Leader | `1-2.2` (hub port 2) | `5B90149286` | `/dev/ttyACM1` | `/dev/serial/by-id/usb-1a86_USB_Single_Serial_5B90149286-if00` |
| Follower | `1-2.1` (hub port 1) | `5B90147327` | `/dev/ttyACM0` | `/dev/serial/by-id/usb-1a86_USB_Single_Serial_5B90147327-if00` |

두 adapter는 USB descriptor만으로 Leader/Follower 역할을 보고하지 않는다. 위 역할은 현재 물리 케이블 배치(hub port 1=Leader, port 2=Follower)에 따른 매핑이다. 배선을 바꾸면 serial number를 기준으로 역할 설정도 함께 갱신한다.

`ModemManager` 서비스가 현재 활성화되어 있다. 진단 당시 포트를 점유하지 않았지만, 재연결 직후 serial probe가 LeRobot과 충돌하면 장치 전용 udev 예외를 검토한다. 시스템 서비스나 udev 설정은 영향 검토 없이 변경하지 않는다.

## USB 카메라

두 카메라의 공통 정보:

- Waveshare 제품: IMX335 5MP USB Camera (B)
- USB descriptor: `Xitech USB Camera`
- USB VID:PID: `0abd:8050`
- `bcdDevice`: `1.05`
- USB 속도: High Speed, 480 Mbps
- Linux driver: `uvcvideo`
- 지원 포맷: MJPEG와 YUYV(YUV 4:2:2)
- 실제 capture 노드는 `video-index0`; `video-index1`은 capture 노드가 아님

| USB 물리 경로 | 현재 capture 노드 | 보조 노드 | 설정에 사용할 안정 경로 |
|---|---|---|---|
| `1-7` (PC root port 7) | `/dev/video2` | `/dev/video3` | `/dev/v4l/by-path/pci-0000:00:14.0-usb-0:7:1.0-video-index0` |
| `1-8` (PC root port 8) | `/dev/video0` | `/dev/video1` | `/dev/v4l/by-path/pci-0000:00:14.0-usb-0:8:1.0-video-index0` |

두 카메라는 동일한 USB serial number `20250606105`를 보고한다. 그 때문에 `/dev/v4l/by-id/usb-Xitech_USB_Camera_20250606105-*` 링크는 한 카메라만 가리키며 재연결 순서에 따라 대상이 달라질 수 있다. 카메라 설정에는 반드시 위 `by-path` 경로를 사용한다.

아직 root port 7/8 중 어느 것이 장면 카메라와 손목 카메라인지는 문서에서 임의 지정하지 않았다. 실제 영상을 확인한 뒤 `scene`/`wrist` 역할을 이 표에 추가한다.

### 실제 지원 스트림 프로필

두 카메라에서 동일하게 V4L2 ioctl로 확인했다.

- MJPEG 30 FPS: `160x120`, `320x240`, `352x288`, `640x480`, `800x600`, `1024x768`, `1280x720`, `1280x960`, `1920x1080`, `2048x1536`, `2592x1944`
- YUYV: `640x480@30`, `800x600@10`, `960x540@10`, `1280x720@10`, `1280x960@5`, `1920x1080@5`, `2048x1536@1`, `2592x1944@1`

### 동시 캡처 확인

2026-08-30 재연결 후 다음 조건으로 GUI 없이 메모리 캡처를 수행했다.

```text
카메라 경로: root port 7 + root port 8
포맷: MJPEG
해상도/FPS: 640x480 @ 30 FPS
결과: 두 카메라 각각 30/30 프레임 성공
VIDIOC_STREAMON 오류: 없음
테스트 후 장치 점유: 없음
```

현재 구성은 VLA 데이터 수집과 ROS 2 카메라 입력의 기본 구성으로 사용할 수 있다. 더 높은 해상도에서는 장시간 frame drop, 실제 FPS, CPU JPEG decode 부하를 별도로 측정한다.

## 과거 hub 연결 문제와 원인

이전에는 두 카메라가 Waveshare USB2 hub의 downstream port 3/4에 함께 연결되어 있었다. 이때 먼저 시작한 카메라만 동작하고 두 번째 카메라는 다음 오류로 실패했다.

```text
VIDIOC_STREAMON = -1 ENOSPC (No space left on device)
```

이는 디스크 공간 문제가 아니라 USB isochronous 대역폭 예약 실패다. 카메라는 MJPEG 해상도가 `160x120`이어도 최대 alt-setting 7, `3072 bytes/125 us`를 예약했다.

```text
카메라 1대 예약: 196.608 Mbps
카메라 2대 예약: 393.216 Mbps
USB2 periodic 예약 한도: 공칭 480 Mbps의 약 80% = 384 Mbps
```

따라서 같은 USB2 hub upstream에서는 MJPEG 해상도 축소만으로 문제가 해결되지 않았다. YUYV bandwidth quirk나 camera firmware 수정은 연구 가능한 우회책이지만, 현재의 PC 직결 구성에서는 필요하지 않다.

Waveshare 공식 데이터셋 문서도 두 arm은 hub에, 두 카메라는 Jetson/호스트에 직접 연결할 것을 권장하며, 카메라를 hub에 연결하면 한 대만 동작할 수 있다고 경고한다.

- SO-ARM100/101 데이터셋: <https://www.waveshare.com/wiki/SO-ARM100/101_Record_Dataset>
- IMX335 5MP USB Camera (B): <https://www.waveshare.com/wiki/IMX335_5MP_USB_Camera_(B)>
- SO-ARM100/101 조립: <https://www.waveshare.com/wiki/SO-ARM100/101_Kit_Aassembly>

공식 IMX335 카메라 Resources에는 AMCap, `mjpg-streamer`, 일반 USB 카메라 예제만 있고 공개 firmware/updater는 확인되지 않았다.

## LeRobot/VLA 설정 원칙

- Arm 포트에는 `/dev/ttyACM*` 대신 `/dev/serial/by-id/*`를 사용한다.
- 카메라에는 `/dev/video*`나 충돌하는 `by-id` 대신 `/dev/v4l/by-path/*-video-index0`를 사용한다.
- 초기 수집 프로필은 두 카메라 `MJPEG 640x480@30`을 권장한다.
- 두 카메라를 별도 capture thread에서 읽고 monotonic timestamp를 보존한다.
- scene/wrist 카메라 역할, intrinsic calibration, 두 카메라와 robot base 사이 extrinsic calibration을 별도 기록한다.
- 학습 데이터 저장용 video compression은 USB 전송이 끝난 뒤 적용된다. H.264나 ROS `compressed` transport는 USB `STREAMON` 대역폭 문제를 해결하지 않는다.

## ROS 2 연동 원칙

- 카메라 capture가 로컬 perception node와 같은 프로세스/호스트라면 raw image와 intra-process 통신을 우선 검토한다.
- 네트워크 또는 원격 구독에만 `image_transport` 압축을 사용한다.
- sensor-data QoS와 작은 queue를 사용해 오래된 프레임이 쌓이지 않도록 한다.
- `header.stamp`, `frame_id`, `camera_info`를 일관되게 관리한다.
- ROS 2 컨테이너를 사용하더라도 기존 cookierunhub Docker 컨테이너, 네트워크, 볼륨을 변경하지 않는다.

## 안전 및 변경 금지 사항

하드웨어 연결 진단만 할 때는 다음을 실행하지 않는다.

- Servo 이동 또는 torque enable
- Calibration 값 생성/덮어쓰기
- Bus Servo Adapter 또는 카메라 firmware flash
- Serial 장치에 임의 패킷 전송
- 기존 LeRobot/ROS 설정 수정
- 기존 운영 Docker 컨테이너 stop/restart/remove
- 사전 검토 없는 `sudo`, driver reload 또는 패키지 설치

## 재연결 후 확인 명령

다음 명령은 장치를 제어하지 않는 read-only 확인용이다.

```bash
lsusb
lsusb -t
ls -l /dev/serial/by-id /dev/serial/by-path
ls -l /dev/v4l/by-path
udevadm info --query=property --name=/dev/ttyACM0
udevadm info --query=property --name=/dev/video0
fuser /dev/ttyACM0 /dev/ttyACM1 /dev/video0 /dev/video2
```

## Arm role mapping (verified 2026-08-30)

Physical motion was verified against the encoder readings: the arm connected through
USB serial `5B90147327` is the **Follower**, and `5B90149286` is the **Leader**.
The software configuration, calibration scripts, and udev aliases use this mapping.
Keep the serial-based mapping when USB hub ports or `/dev/ttyACM*` numbers change.
If the physical arm wiring changes, update all three locations together before
calibrating or enabling teleoperation.
