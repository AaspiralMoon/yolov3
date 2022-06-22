import onnxruntime as ort
import cv2
import numpy as np
import torch
import torchvision
from flask import Flask


def letterbox(im, new_shape=(640, 640), color=(114, 114, 114), auto=True, scaleFill=False, scaleup=True, stride=32):
    # Resize and pad image while meeting stride-multiple constraints
    shape = im.shape[:2]  # current shape [height, width]
    if isinstance(new_shape, int):
        new_shape = (new_shape, new_shape)

    # Scale ratio (new / old)
    r = min(new_shape[0] / shape[0], new_shape[1] / shape[1])
    if not scaleup:  # only scale down, do not scale up (for better val mAP)
        r = min(r, 1.0)

    # Compute padding
    ratio = r, r  # width, height ratios
    new_unpad = int(round(shape[1] * r)), int(round(shape[0] * r))
    dw, dh = new_shape[1] - new_unpad[0], new_shape[0] - new_unpad[1]  # wh padding
    if auto:  # minimum rectangle
        dw, dh = np.mod(dw, stride), np.mod(dh, stride)  # wh padding
    elif scaleFill:  # stretch
        dw, dh = 0.0, 0.0
        new_unpad = (new_shape[1], new_shape[0])
        ratio = new_shape[1] / shape[1], new_shape[0] / shape[0]  # width, height ratios

    dw /= 2  # divide padding into 2 sides
    dh /= 2

    if shape[::-1] != new_unpad:  # resize
        im = cv2.resize(im, new_unpad, interpolation=cv2.INTER_LINEAR)
    top, bottom = int(round(dh - 0.1)), int(round(dh + 0.1))
    left, right = int(round(dw - 0.1)), int(round(dw + 0.1))
    im = cv2.copyMakeBorder(im, top, bottom, left, right, cv2.BORDER_CONSTANT, value=color)  # add border
    return im, ratio, (dw, dh)


def box_iou(box1, box2):
    # https://github.com/pytorch/vision/blob/master/torchvision/ops/boxes.py
    """
    Return intersection-over-union (Jaccard index) of boxes.
    Both sets of boxes are expected to be in (x1, y1, x2, y2) format.
    Arguments:
        box1 (Tensor[N, 4])
        box2 (Tensor[M, 4])
    Returns:
        iou (Tensor[N, M]): the NxM matrix containing the pairwise
            IoU values for every element in boxes1 and boxes2
    """

    def box_area(box):
        # box = 4xn
        return (box[2] - box[0]) * (box[3] - box[1])

    area1 = box_area(box1.T)
    area2 = box_area(box2.T)

    # inter(N,M) = (rb(N,M,2) - lt(N,M,2)).clamp(0).prod(2)
    inter = (torch.min(box1[:, None, 2:], box2[:, 2:]) - torch.max(box1[:, None, :2], box2[:, :2])).clamp(0).prod(2)
    return inter / (area1[:, None] + area2 - inter)  # iou = inter / (area1 + area2 - inter)


def xywh2xyxy(x):
    # Convert nx4 boxes from [x, y, w, h] to [x1, y1, x2, y2] where xy1=top-left, xy2=bottom-right
    y = x.clone() if isinstance(x, torch.Tensor) else np.copy(x)
    y[:, 0] = x[:, 0] - x[:, 2] / 2  # top left x
    y[:, 1] = x[:, 1] - x[:, 3] / 2  # top left y
    y[:, 2] = x[:, 0] + x[:, 2] / 2  # bottom right x
    y[:, 3] = x[:, 1] + x[:, 3] / 2  # bottom right y
    return y


def non_max_suppression(prediction, conf_thres=0.25, iou_thres=0.45, classes=None, agnostic=False, multi_label=False,
                        labels=(), max_det=300):
    """Runs Non-Maximum Suppression (NMS) on inference results

    Returns:
         list of detections, on (n,6) tensor per image [xyxy, conf, cls]
    """

    nc = prediction.shape[2] - 5  # number of classes
    xc = prediction[..., 4] > conf_thres  # candidates

    # Checks
    assert 0 <= conf_thres <= 1, f'Invalid Confidence threshold {conf_thres}, valid values are between 0.0 and 1.0'
    assert 0 <= iou_thres <= 1, f'Invalid IoU {iou_thres}, valid values are between 0.0 and 1.0'

    # Settings
    min_wh, max_wh = 2, 4096  # (pixels) minimum and maximum box width and height
    max_nms = 30000  # maximum number of boxes into torchvision.ops.nms()
    time_limit = 10.0  # seconds to quit after
    redundant = True  # require redundant detections
    multi_label &= nc > 1  # multiple labels per box (adds 0.5ms/img)
    merge = False  # use merge-NMS

    output = [torch.zeros((0, 6), device=prediction.device)] * prediction.shape[0]
    for xi, x in enumerate(prediction):  # image index, image inference
        # Apply constraints
        # x[((x[..., 2:4] < min_wh) | (x[..., 2:4] > max_wh)).any(1), 4] = 0  # width-height
        x = x[xc[xi]]  # confidence

        # Cat apriori labels if autolabelling
        if labels and len(labels[xi]):
            l = labels[xi]
            v = torch.zeros((len(l), nc + 5), device=x.device)
            v[:, :4] = l[:, 1:5]  # box
            v[:, 4] = 1.0  # conf
            v[range(len(l)), l[:, 0].long() + 5] = 1.0  # cls
            x = torch.cat((x, v), 0)

        # If none remain process next image
        if not x.shape[0]:
            continue

        # Compute conf
        x[:, 5:] *= x[:, 4:5]  # conf = obj_conf * cls_conf

        # Box (center x, center y, width, height) to (x1, y1, x2, y2)
        box = xywh2xyxy(x[:, :4])

        # Detections matrix nx6 (xyxy, conf, cls)
        if multi_label:
            i, j = (x[:, 5:] > conf_thres).nonzero(as_tuple=False).T
            x = torch.cat((box[i], x[i, j + 5, None], j[:, None].float()), 1)
        else:  # best class only
            conf, j = x[:, 5:].max(1, keepdim=True)
            x = torch.cat((box, conf, j.float()), 1)[conf.view(-1) > conf_thres]

        # Filter by class
        if classes is not None:
            x = x[(x[:, 5:6] == torch.tensor(classes, device=x.device)).any(1)]

        # Apply finite constraint
        # if not torch.isfinite(x).all():
        #     x = x[torch.isfinite(x).all(1)]

        # Check shape
        n = x.shape[0]  # number of boxes
        if not n:  # no boxes
            continue
        elif n > max_nms:  # excess boxes
            x = x[x[:, 4].argsort(descending=True)[:max_nms]]  # sort by confidence

        # Batched NMS
        c = x[:, 5:6] * (0 if agnostic else max_wh)  # classes
        boxes, scores = x[:, :4] + c, x[:, 4]  # boxes (offset by class), scores
        i = torchvision.ops.nms(boxes, scores, iou_thres)  # NMS
        if i.shape[0] > max_det:  # limit detections
            i = i[:max_det]
        if merge and (1 < n < 3E3):  # Merge NMS (boxes merged using weighted mean)
            # update boxes as boxes(i,4) = weights(i,n) * boxes(n,4)
            iou = box_iou(boxes[i], boxes) > iou_thres  # iou matrix
            weights = iou * scores[None]  # box weights
            x[i, :4] = torch.mm(weights, x[:, :4]).float() / weights.sum(1, keepdim=True)  # merged boxes
            if redundant:
                i = i[iou.sum(1) > 1]  # require redundancy

        output[xi] = x[i]

    return output


app = Flask(__name__)


@app.route("/predict")
def predict():
    # pre-process input images
    img0 = cv2.imread('data/images/bus.jpg')  # BGR
    img = letterbox(img0, new_shape=(640, 640), auto=False, stride=32)[0]  # Padded resize
    img = img.transpose((2, 0, 1))[::-1]  # HWC to CHW, BGR to RGB
    img = np.ascontiguousarray(img)
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    img = torch.from_numpy(img).to(device)
    img = img.float()  # uint8 to fp16/32
    img /= 255  # 0 - 255 to 0.0 - 1.0
    img = img[None]
    img = img.cpu().numpy()

    # load onnx file and run inferece
    b, ch, h, w = img.shape  # batch, channel, height, width
    providers = ['CUDAExecutionProvider', 'CPUExecutionProvider']
    session = ort.InferenceSession('weights/yolov3.onnx', providers=providers)
    pred = session.run([session.get_outputs()[0].name], {session.get_inputs()[0].name: img})[0]
    pred[..., 0] *= w  # x
    pred[..., 1] *= h  # y
    pred[..., 2] *= w  # w
    pred[..., 3] *= h  # h
    pred = torch.tensor(pred)
    print(pred.shape)

    # NMS 
    pred = non_max_suppression(pred)
    # return(pred)
    return 'App OK'


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)

# # Process predictions
# for i, det in enumerate(pred):  # per image
# if webcam:  # batch_size >= 1
#     p, im0, frame = path[i], im0s[i].copy(), dataset.count
#     s += f'{i}: '
# else:
#     p, im0, frame = path, im0s.copy(), getattr(dataset, 'frame', 0)

# p = Path(p)  # to Path
# save_path = str(save_dir / p.name)  # im.jpg
# txt_path = str(save_dir / 'labels' / p.stem) + ('' if dataset.mode == 'image' else f'_{frame}')  # im.txt
# s += '%gx%g ' % im.shape[2:]  # print string
# gn = torch.tensor(im0.shape)[[1, 0, 1, 0]]  # normalization gain whwh
# imc = im0.copy() if save_crop else im0  # for save_crop
# annotator = Annotator(im0, line_width=line_thickness, example=str(names))
# if len(det):
#     # Rescale boxes from img_size to im0 size
#     det[:, :4] = scale_coords(im.shape[2:], det[:, :4], im0.shape).round()

#     # Print results
#     for c in det[:, -1].unique():
#         n = (det[:, -1] == c).sum()  # detections per class
#         s += f"{n} {names[int(c)]}{'s' * (n > 1)}, "  # add to string

#     # Write results
#     for *xyxy, conf, cls in reversed(det):
#         if save_txt:  # Write to file
#             xywh = (xyxy2xywh(torch.tensor(xyxy).view(1, 4)) / gn).view(-1).tolist()  # normalized xywh
#             line = (cls, *xywh, conf) if save_conf else (cls, *xywh)  # label format
#             with open(txt_path + '.txt', 'a') as f:
#                 f.write(('%g ' * len(line)).rstrip() % line + '\n')

#         if save_img or save_crop or view_img:  # Add bbox to image
#             c = int(cls)  # integer class
#             label = None if hide_labels else (names[c] if hide_conf else f'{names[c]} {conf:.2f}')
#             annotator.box_label(xyxy, label, color=colors(c, True))
#             if save_crop:
#                 save_one_box(xyxy, imc, file=save_dir / 'crops' / names[c] / f'{p.stem}.jpg', BGR=True)

# # Save results (image with detections)
# if save_img:
#     if dataset.mode == 'image':
#         cv2.imwrite(save_path, im0)
#     else:  # 'video' or 'stream'
#         if vid_path[i] != save_path:  # new video
#             vid_path[i] = save_path
#             if isinstance(vid_writer[i], cv2.VideoWriter):
#                 vid_writer[i].release()  # release previous video writer
#             if vid_cap:  # video
#                 fps = vid_cap.get(cv2.CAP_PROP_FPS)
#                 w = int(vid_cap.get(cv2.CAP_PROP_FRAME_WIDTH))
#                 h = int(vid_cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
#             else:  # stream
#                 fps, w, h = 30, im0.shape[1], im0.shape[0]
#                 save_path += '.mp4'
#             vid_writer[i] = cv2.VideoWriter(save_path, cv2.VideoWriter_fourcc(*'mp4v'), fps, (w, h))
#         vid_writer[i].write(im0)
# Results