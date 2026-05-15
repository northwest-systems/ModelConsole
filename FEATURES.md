# FEATURES

## 権限管理

### ファイルアクセス管理

mcon は、エージェントごとに異なるファイルアクセス権限を適用する。

ファイルアクセスは POSIX ACL により制御する。

デフォルトの権限は `Ignore` とする。

#### Ignore

`Ignore` は、存在とメタデータのみ確認できる状態を表す。

できること:

- `ls` で名前を見る
- `stat` でメタデータを見る
- ファイルやディレクトリの存在を確認する

できないこと:

- ファイル内容を読む
- ファイルへ書き込む
- 既存ファイルを削除または rename する

#### Read

`Read` は、既存ファイルの読み取りを許可する。

できること:

- `Ignore` でできること
- ファイル内容を読む

できないこと:

- 既存ファイルへ書き込む
- 既存ファイルを削除または rename する

#### Write

`Write` は、許可されたディレクトリ内で新規ファイルを作成する権限を表す。

できること:

- 新規ファイルを作成する

できないこと:

- 既存ファイルを読み取る
- 既存ファイルを上書きする
- 既存ファイルを削除または rename する

`Write` により作成されたファイルは、作成直後に root owner へ変更される。

その際、作成したエージェントには対象ファイルの `Edit` 権限が付与される。

この `Edit` 権限は一時的な追加権限として扱う。

次回以降の新規セッション開始時、ACL Manager は現在の policy を再適用し、policy に含まれない追加権限を削除する。

#### Edit

`Edit` は、既存ファイルの読み取りと書き込みを許可する。

できること:

- `Read` でできること
- 既存ファイルへ書き込む

#### ACL Manager

server は ACL Manager として、エージェントごとの権限を workspace に適用する。

ACL Manager の責務:

- policy を POSIX ACL に変換する
- agent ごとの ACL を設定する
- 親ディレクトリに必要な traversal 権限を設定する
- `Write` ディレクトリに新規作成権限を設定する
- `Write` により作成されたファイルを root owner へ変更する
- 作成したエージェントに対象ファイルの `Edit` 権限を付与する
- 新規セッション開始時に現在の policy を再適用する

#### Host And Root

root は host と同じ UID を指す。

root は通常どおり workspace を操作できる。

server は権限を管理する主体として扱う。

#### Examples

##### Ignore

```python
from pathlib import Path

p = Path("secret.env")

p.exists()        # True
p.stat()          # OK
p.read_text()     # PermissionError
p.write_text("x") # PermissionError
```

##### Read

```python
from pathlib import Path

p = Path("README.md")

p.exists()        # True
p.read_text()     # OK
p.write_text("x") # PermissionError
```

##### Write

```python
from pathlib import Path

new_file = Path("generated/new.txt")

new_file.write_text("hello")   # OK

new_file.read_text()           # OK after temporary Edit is granted
new_file.write_text("updated") # OK after temporary Edit is granted
new_file.unlink()              # PermissionError unless deletion is separately allowed

Path("generated/existing.txt").write_text("x") # PermissionError unless Edit is granted
```

##### Edit

```python
from pathlib import Path

p = Path("packages/server/app.py")

p.read_text()     # OK
p.write_text("x") # OK
```
