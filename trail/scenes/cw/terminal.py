from __future__ import annotations

import re
from trail.scenes.cw.stage import _read_ocr_box, _read_ocr_piece
from trail.runtime.ocr_config import OcrRequestConfig


def _in(piece, frame, region):
    box = _read_ocr_box(piece)
    if not box or frame is None:
        return False
    x, y = (box[0] + box[2]) / (2 * frame.width), (box[1] + box[3]) / (2 * frame.height)
    return region[0] <= x <= region[2] and region[1] <= y <= region[3]


def _texts(observation, region):
    return [_read_ocr_piece(p).replace(' ', '') for p in observation.page_ocr_pieces if _in(p, observation.frame, region)]


def _has(observation, region, *texts):
    return any(any(t in p for t in texts) for p in _texts(observation, region))


def classify_terminal(observation):
    if observation.frame is None:
        return None
    if (_has(observation, (.37,.14,.63,.27), '挑战成功', '挑战失败')
        and _has(observation, (.40,.31,.60,.40), '对局评价')):
        return 'terminal_evaluation'
    if (_has(observation, (.20,.14,.34,.23), '对局胜利', '对局失败')
        and _has(observation, (.38,.34,.50,.40), '我的阵容')
        and _has(observation, (.14,.47,.39,.67), '奖励信息', '积分')):
        return 'terminal_detail'
    return None


def looks_like_terminal(observation):
    if observation.frame is None:
        return False
    return (_has(observation, (.20,.14,.34,.23), '对局胜利', '对局失败')
            or _has(observation, (.38,.34,.50,.40), '我的阵容')
            or _has(observation, (.40,.31,.60,.40), '对局评价')
            or (_has(observation, (.37,.14,.63,.27), '挑战成功', '挑战失败')
                and _has(observation, (.40,.79,.60,.88), '下一步')))


def next_point(observation):
    candidates = [p for p in observation.page_ocr_pieces
                  if _read_ocr_piece(p).replace(' ', '') == '下一步'
                  and _in(p, observation.frame, (.40,.79,.60,.88))]
    if len(candidates) != 1:
        return None
    box = _read_ocr_box(candidates[0])
    return (round((box[0]+box[2])/2 / observation.frame.width * 1920),
            round((box[1]+box[3])/2 / observation.frame.height * 1080))


def _region_ocr(runtime, frame, region):
    box = tuple(round(v * (frame.width if i % 2 == 0 else frame.height)) for i, v in enumerate(region))
    crop = frame.crop(box)
    return ''.join(_read_ocr_piece(p).replace(' ', '') for p in (runtime.ocr_image(crop.resize((crop.width*2,crop.height*2)), ocr=OcrRequestConfig(lang='ch', ocr_mode='high')) or []))


def extract_terminal(runtime, observation, kind):
    frame = observation.frame
    pieces = observation.page_ocr_pieces
    def text(region): return ''.join(_texts(observation, region))
    headline = text((.2,.14,.65,.27))
    wins = '挑战成功' in headline or '对局胜利' in headline
    losses = '挑战失败' in headline or '对局失败' in headline
    result = ('win' if wins else 'lose') if wins != losses else None
    grade_region = (.40,.40,.60,.65) if kind == 'terminal_evaluation' else (.20,.23,.34,.40)
    grade_text = text(grade_region)
    grade = next((g for g in ('SSS','SS','S','A','B','C') if re.fullmatch(g, grade_text, re.I)), None)
    if grade is None:
        grade_text = _region_ocr(runtime, frame, grade_region)
        grade = next((g for g in ('SSS','SS','S','A','B','C') if re.fullmatch(g, grade_text, re.I)), None)
    hp_region = (.43,.25,.57,.30) if kind == 'terminal_evaluation' else (.72,.16,.85,.21)
    hp_text = text(hp_region)
    hp = re.search(r'小队生命值\D*(\d{1,3})', hp_text)
    if not hp:
        hp_text = _region_ocr(runtime, frame, hp_region)
        hp = re.search(r'(?:小队生命值\D*)?(\d{1,3})', hp_text)
    hp_value = int(hp[1]) if hp and int(hp[1]) <= 100 else None
    result_data = {'result': result, 'grade': grade, 'hp': hp_value}
    if kind == 'terminal_detail':
        node_text = text((.39,.15,.50,.22))
        node = re.search(r'(?<!\d)([1-3]-\d{1,2})(?!\d)', node_text)
        mode_text = text((.16,.42,.37,.47))
        if '标准博弈' not in mode_text:
            mode_text = _region_ocr(runtime, frame, (.16,.42,.37,.47))
        difficulty = re.search(r'(A\d+)财富造物主\s*(\d+)', mode_text, re.I)
        score_text = text((.14,.60,.39,.66))
        score = re.search(r'积分\D*(\d+)', score_text)
        if not score:
            digits = [t for t in _texts(observation, (.32,.60,.39,.66)) if re.fullmatch(r'\d+', t)]
            score_value = int(digits[0]) if len(digits) == 1 else None
        else:
            score_value = int(score[1])
        result_data.update(node=node[1] if node else None, boss='首领' if '首领' in node_text else None,
                           mode='标准博弈' if '标准博弈' in mode_text else None,
                           difficulty=(difficulty[1]+'-'+difficulty[2]) if difficulty else None, score=score_value)
    return result_data
