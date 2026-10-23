# Tests that run FSUDAQ's board and data code against a fake CAEN library (no hardware).
# Needs only the CAENDigitizer headers (installed in /usr/include by CAEN's installer).
#
#   make -C tests            build and run every test
#   make -C tests CAEN_INC=/path/to/CAENDigitizer/include

CAEN_INC ?= /usr/include
CXX      ?= g++
CXXFLAGS ?= -std=c++17 -g -O1 -Wall -Wextra -Wno-unused-parameter -Wno-sign-compare
CPPFLAGS += -I.. -I. -I$(CAEN_INC)

TESTS = test_settings

.PHONY: all run clean
all: run

fake_caen.o: fake_caen.cpp fake_caen.h
	$(CXX) $(CXXFLAGS) $(CPPFLAGS) -c $< -o $@

ClassDigitizer.o: ../ClassDigitizer.cpp ../ClassDigitizer.h ../ClassData.h ../RegisterAddress.h ../macro.h
	$(CXX) $(CXXFLAGS) $(CPPFLAGS) -c $< -o $@

test_settings: test_settings.cpp ClassDigitizer.o fake_caen.o
	$(CXX) $(CXXFLAGS) $(CPPFLAGS) $^ -o $@

run: $(TESTS)
	@cd .. && for t in $(TESTS); do echo "##### $$t"; tests/$$t > tests/$$t.log 2>&1; rc=$$?; grep -E "^(   FAIL|   ok|== |PASSED|FAILED)" tests/$$t.log; [ $$rc -eq 0 ] || exit $$rc; done

clean:
	rm -f *.o *.log $(TESTS)
