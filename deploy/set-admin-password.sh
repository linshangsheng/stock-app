#!/usr/bin/env bash
# 部署后第一件事：把管理员 root 的默认密码（123456）改成你自己的。
# 用法：在服务器上，用运行软件的那个用户执行   bash set-admin-password.sh
#   · 密码要「手敲」，不要粘贴（网页终端粘贴时会夹带看不见的控制符号，脚本会检查并拒绝）。
#   · 密码只经过本机的 127.0.0.1，不写进任何文件、不进命令历史。
#   · 改好后会自己验证：旧密码应该登录不了，新密码应该能登录。
set -u
URL="${STOCK_URL:-http://127.0.0.1:8000}"
JAR="$(mktemp)"; trap 'rm -f "$JAR"' EXIT

read -r -s -p "新密码（至少 6 位，手敲，输入时不显示）: " P1; echo
read -r -s -p "再输一遍: " P2; echo
[ "$P1" = "$P2" ]      || { echo "两次不一样，没有修改。"; exit 1; }
[ "${#P1}" -ge 6 ]     || { echo "至少 6 位，没有修改。"; exit 1; }
[ "$P1" != "123456" ]  || { echo "不能还是默认密码 123456，没有修改。"; exit 1; }
case "$P1" in *[![:print:]]*) echo "密码里有看不见的字符（多半是粘贴造成的），请手敲。没有修改。"; exit 1;; esac

json() { python3 -c 'import json,os,sys; print(json.dumps(dict(zip(sys.argv[1:], (os.environ[k] for k in sys.argv[1:])))))' "$@"; }
login_code() { printf '%s' "$1" | curl -s -o /dev/null -w "%{http_code}" -c "$JAR" -H "Content-Type: application/json" --data-binary @- "$URL/api/auth/login"; }

body=$(U=root P=123456 json U P | sed 's/"U"/"username"/; s/"P"/"password"/')
[ "$(login_code "$body")" = 200 ] || { echo "用默认密码登录失败：可能已经改过了，或者软件没在运行。"; exit 1; }

chg=$(OLD=123456 NEW="$P1" json OLD NEW | sed 's/"OLD"/"old"/; s/"NEW"/"new"/')
res=$(printf '%s' "$chg" | curl -s -b "$JAR" -H "Content-Type: application/json" --data-binary @- "$URL/api/auth/password")
echo "$res" | grep -q '"ok": *true' || { echo "修改失败：$res"; exit 1; }

old_code=$(login_code "$body")
new_body=$(U=root P="$P1" json U P | sed 's/"U"/"username"/; s/"P"/"password"/')
new_code=$(login_code "$new_body")
echo "旧密码 123456 登录：$old_code（401 = 已失效，正确）"
echo "新密码登录：$new_code（200 = 可以用，正确）"
[ "$old_code" = 401 ] && [ "$new_code" = 200 ] && echo "完成。现在可以开放 80 / 443 了。" || echo "结果不对，请把上面两行发给我。"
