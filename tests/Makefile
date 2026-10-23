# Tests that run FSUDAQ's board and data code against a fake CAEN library (no hardware).
# Needs the CAENDigitizer headers (installed in /usr/include by CAEN's installer); test_daq
# also needs Qt 6 (the packages FSUDAQ itself is built with).
#
#   make -C tests            build and run every test
#   make -C tests CAEN_INC=/path/to/CAENDigitizer/include

CAEN_INC ?= /usr/include
CXX      ?= g++
CXXFLAGS ?= -std=c++17 -g -O1 -Wall -Wextra -Wno-unused-parameter -Wno-sign-compare
CPPFLAGS += -I.. -I. -I$(CAEN_INC)
DEPFLAGS  = -MMD -MP

QT_MODULES = Qt6Widgets Qt6Charts Qt6PrintSupport Qt6Network
QT_CFLAGS ?= $(shell pkg-config --cflags $(QT_MODULES))
QT_LIBS   ?= $(shell pkg-config --libs $(QT_MODULES))
MOC       ?= /usr/lib/qt6/libexec/moc

TESTS = test_settings test_daq

.PHONY: all run clean
all: run

fake_caen.o: fake_caen.cpp fake_caen.h
	$(CXX) $(CXXFLAGS) $(CPPFLAGS) $(DEPFLAGS) -fPIC -c $< -o $@

ClassDigitizer.o: ../ClassDigitizer.cpp ../ClassDigitizer.h ../ClassData.h ../RegisterAddress.h ../macro.h
	$(CXX) $(CXXFLAGS) $(CPPFLAGS) $(DEPFLAGS) -fPIC -c $< -o $@

test_settings: test_settings.cpp ClassDigitizer.o fake_caen.o
	$(CXX) $(CXXFLAGS) $(CPPFLAGS) $^ -o $@

#---- test_daq: FSUDAQ itself (all of it but main.cpp) linked with the fake library
GUI_SOURCES = DigiSettingsPanel.cpp FSUDAQ.cpp ClassInfluxDB.cpp Scope.cpp SingleSpectra.cpp MultiBuilder.cpp qcustomplot.cpp analyzers/Analyser.cpp
GUI_HEADERS = CustomThreads.h DigiSettingsPanel.h CustomWidgets.h Scope.h SingleSpectra.h Histogram1D.h FSUDAQ.h qcustomplot.h
ANALYZER_MOC = $(patsubst ../analyzers/%.h,build/%.moc.o,$(shell grep -l Q_OBJECT ../analyzers/*.h))

build/%.o: ../%.cpp
	@mkdir -p build
	$(CXX) $(CXXFLAGS) $(CPPFLAGS) $(DEPFLAGS) $(QT_CFLAGS) -fPIC -c $< -o $@
build/%.o: ../analyzers/%.cpp
	@mkdir -p build
	$(CXX) $(CXXFLAGS) $(CPPFLAGS) $(DEPFLAGS) $(QT_CFLAGS) -fPIC -c $< -o $@
build/%.moc.cpp: ../%.h
	@mkdir -p build
	$(MOC) $(CPPFLAGS) $(filter -I% -D%,$(QT_CFLAGS)) $< -o $@
build/%.moc.cpp: ../analyzers/%.h
	@mkdir -p build
	$(MOC) $(CPPFLAGS) $(filter -I% -D%,$(QT_CFLAGS)) $< -o $@
build/%.moc.o: build/%.moc.cpp
	$(CXX) $(CXXFLAGS) $(CPPFLAGS) $(DEPFLAGS) $(QT_CFLAGS) -fPIC -c $< -o $@
build/test_daq.o: test_daq.cpp fake_caen.h ../FSUDAQ.h
	@mkdir -p build
	$(CXX) $(CXXFLAGS) $(CPPFLAGS) $(DEPFLAGS) $(QT_CFLAGS) -fPIC -c $< -o $@

test_daq: build/test_daq.o ClassDigitizer.o fake_caen.o $(addprefix build/, $(notdir $(GUI_SOURCES:.cpp=.o))) $(addprefix build/, $(GUI_HEADERS:.h=.moc.o)) $(ANALYZER_MOC)
	$(CXX) $(CXXFLAGS) $^ $(QT_LIBS) -lcurl -lpthread -o $@

run: $(TESTS)
	@cd .. && for t in $(TESTS); do echo "##### $$t"; QT_QPA_PLATFORM=offscreen tests/$$t > tests/$$t.log 2>&1; rc=$$?; grep -E "^(   FAIL|   ok|== |PASSED|FAILED)" tests/$$t.log; [ $$rc -eq 0 ] || exit $$rc; done

-include $(wildcard *.d build/*.d)

clean:
	rm -rf *.o *.d *.log build $(TESTS)
