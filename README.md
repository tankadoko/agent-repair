# Agent Repair

失敗したセッションの履歴から、スキルやAutomationの指示を修正します。Codexで使うスキルとPythonハーネスです。

[Closing the Consistency Gap: Self-Evolving Agents That Learn to Stay on Course](https://arxiv.org/abs/2609.08832) に着想を得ています。

## 使い方

Python 3.11以上が必要です。

このリポジトリをCodexで開き、`$repair-session` を指定します。以後は同じタスクに、次のように送ってください。

```text
<セッションID>
失敗しているようなので直しておいて。
```

修正前後の動作を確認してから、変更を反映します。

[MIT License](LICENSE)
