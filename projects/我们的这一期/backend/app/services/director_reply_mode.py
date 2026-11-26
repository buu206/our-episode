"""Conservative presentation routing; never grants a tool or executes a task."""
import re


def _question(context):
    value = context.get('question', '') if isinstance(context, dict) else ''
    return value if isinstance(value, str) else ''


def discussion_only(context):
    question = _question(context)
    # Mixed/ambiguous requests retain the existing action path. In particular,
    # "先不执行" must not suppress an explicitly requested pending proposal.
    if re.search(r'卡|试剪|执行|生成|打开|查看|回看|带我|跳转|方案|旁白|字幕|给.{0,12}剪法', question):
        return False
    if re.search(r'(?:不要|别|不必|不用)只', question):
        return False
    return bool(re.search(r'只(?:解释|聊|回答|分析|说|用一句话)|(?:不要|先不)改片', question))


def reply_requirements(context):
    question = _question(context)
    if (re.search(r'(?:只用|只说|用|说)一句话', question)
            and not re.search(r'(?:不要|别|不必|不用)(?:只用|只说|用|说)?一句话', question)
            and not re.search(r'详细|详解|展开|逐项', question)):
        return ('本轮只答一句话，最多50字、一个重点；将建议与必要的不确定性放进同一句，'
                '不追加第二句说明，不报用户未问的时点。不要省略会改变判断的限制。'
                '\n下面是虚构表达示例，不是本期素材：抽样只看到伸手，未看清接球，计划花字为“接到了”。'
                '示范答法：“我先改‘接到了’，抽样还没看清是否接住，先别替结果下结论。”'
                '学习句子长度和限定方式，不照搬示例物品、动作或文字。')
    if re.search(r'详细|详解|展开|逐项|先后依据|哪些.{0,6}不能确定', question):
        return ('本轮按用户所问展开：先回答判断，再说明依据及不确定性，最多3个短段；'
                '每段只讲一件事，必要时用短列表，不设50/120字限制。'
                '计划与抽样的限定须贯穿结论，不能前文说待核实、结尾又改称已经确认。'
                '\n表述对照（不是本期事实）：前文若是“按计划显示”，错误收尾是“所以确认花字出现”；'
                '正确收尾是“这里只能比较计划时段与抽样动作，实际显示仍需核对”。'
                '解释当前素材的具体依据，不逐字照抄这个示例。')
    return ('本轮普通回答只选最重要的一处，60—100字、最多120字、1—2短段；'
            '给一个判断和一个建议，不追加另一处或备选菜单。必要的不确定性贴着该判断说。'
            '段间用\\n\\n，不强制标题，不复述已知背景；没问具体位置就不报时间数字。'
            '\n下面是虚构表达示例，不是本期素材：抽样只看到伸手，未看清接球，计划花字为“接到了”。'
            '示范答法：“我先改‘接到了’这句：按现有计划，它替还没看清的结果下了结论；'
            '先换成不判结果的短词，动作是否成功再回看。”'
            '学习一个重点与依据相连的写法，不照搬示例物品、动作或文字。')
