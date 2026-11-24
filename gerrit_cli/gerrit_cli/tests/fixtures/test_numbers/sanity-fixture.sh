#!/bin/bash
ONLY=${ONLY:-"$*"}
ALWAYS_EXCEPT="$SANITY_EXCEPT 9"
always_except LU-1 12

test_1() {
	true
}
run_test 1 "one"

# comment for 2
test_2() {
	true
}
run_test 2 "two"

test_8() {
	true
}
run_test 8 "eight"

test_10() {
	true
}
run_test 10 "ten"

test_13() {
	true
}
run_test 13 "thirteen"

complete_test $SECONDS
exit_status
