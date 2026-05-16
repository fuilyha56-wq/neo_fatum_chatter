# KFC 提示词拼接修复 - 变更总结

## 问题描述

KFC 有时会出现"未出现有效文本"的回复，根因是系统提示词模板中 `personality_core`、`personality_side`、`identity` 三个字段在配置为空时，模板的骨架文字（前缀/后缀）残留在提示词中，产生语义残缺的行。

## 根因分析

原始模板（`prompts/templates.py` 第100-103行）：
```
# 核心设定
你{personality_core}
{personality_side}。
你的身份是{identity}。
```

当字段为空时，`optional` 策略返回空字符串 `""`，导致：

| 空字段 | 渲染结果 | 问题 |
|---|---|---|
| `personality_core=""` | `你` | 只有"你"两个字，后面没有描述 |
| `personality_side=""` | `。` | 孤立句号，没有前面的描述 |
| `identity=""` | `你的身份是。` | "你的身份是"后直接接句号 |

这些残缺文本让模型无法理解人设描述，导致回复"未出现有效文本"。

## 修复方案

将骨架文字从模板硬编码改为由策略链动态包裹，空值时整行完全消失。

### 修改文件

1. **`prompts/templates.py`**：模板中核心设定区块
   - 旧：`你{personality_core}\n{personality_side}。\n你的身份是{identity}。`
   - 新：`{personality_core_line}{personality_side_line}{identity_line}`

2. **`prompts/modules.py`**：新增三个策略链
   - `personality_core_line`: `optional(core).then(min_len(1)).then(wrap("你", "\n"))`
   - `personality_side_line`: `optional(side).then(min_len(1)).then(wrap("", "。\n"))`
   - `identity_line`: `optional(identity).then(min_len(1)).then(wrap("你的身份是", "。\n"))`

### 策略链逻辑

1. `optional()` → 空值返回 `""`，非空返回原始值
2. `min_len(1)` → strip 后长度 < 1 返回 `""`，防止纯空格通过
3. `wrap()` → 空值返回 `""`（骨架也消失），非空包裹前缀+值+后缀+换行

### 渲染效果对比

| 场景 | 旧模板 | 新模板 |
|---|---|---|
| 全有值 | `你温柔\n善良。\n你的身份是大学生。` | `你温柔\n善良。\n你的身份是大学生。\n` |
| core 空 | `你\n善良。\n你的身份是大学生。` | `善良。\n你的身份是大学生。\n` |
| side 空 | `你温柔\n。\n你的身份是大学生。` ← 孤立句号 | `你温柔\n你的身份是大学生。\n` |
| 全空 | `你\n。\n你的身份是。` ← 全是残缺 | `""`（完全消失） |

## 影响范围

- 仅影响系统提示词中 `<personality>` 区块的渲染
- 不影响其他模板或运行时逻辑
- 向后兼容：有值时渲染结果与原来一致（仅末尾多一个换行符，不影响模型理解）
