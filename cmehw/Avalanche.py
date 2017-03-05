import os, time, glob, json

from collections import deque


import RPi.GPIO as GPIO
import spidev

from .common import Config

# load the STPM3X module and import Stpm3x class
from .STPM3X import Stpm3x

# GPIO assignments
AVALANCHE_GPIO_SENSOR_POWER     = 5
AVALANCHE_GPIO_ISOLATE_SPI_BUS  = 6

AVALANCHE_GPIO_SYNC_SENSOR0     = 12

AVALANCHE_GPIO_RELAY_CHANNEL1   = 28
AVALANCHE_GPIO_RELAY_CHANNEL2   = 29
AVALANCHE_GPIO_RELAY_CHANNEL3   = 30
AVALANCHE_GPIO_RELAY_CHANNEL4   = 31

AVALANCHE_GPIO_LED1 = 32
AVALANCHE_GPIO_LED2 = 33
AVALANCHE_GPIO_LED3 = 34
AVALANCHE_GPIO_LED4 = 35
AVALANCHE_GPIO_LED5 = 36

AVALANCHE_GPIO_STPM34_ENABLE	= 21

AVALANCHE_GPIO_SPI_CE0		    = 8
AVALANCHE_GPIO_SPI_CE1		    = 7

AVALANCHE_GPIO_MUX_S0   		= 13
AVALANCHE_GPIO_MUX_S1   		= 14

AVALANCHE_GPIO_U3_MISO_EN		= 20
AVALANCHE_GPIO_U7_MISO_EN		= 26

AVALANCHE_GPIO_MUX_PUPD_CNTL    = 19

# Discharge sensors for this long before enabling SPI bus
SPI_BUS_DISCHARGE_WAIT_s = 10

# Hardware channels configurations stored here
CHDIR = Config.CHDIR

BUFFER_POINTS = Config.BUFFER_POINTS


class _Sensor:
	def __init__(self, id, sensor_type, unit, sensor_range, read_function):
		self.id = id
		self.type = sensor_type
		self.unit = unit
		self.range = sensor_range
		self._read = read_function

		# keep a buffer of values
		# new values are pushed at left (values[0])
		# and oldest value falls off right
		self.values = deque([None for x in range(BUFFER_POINTS)]) 

	def read(self, tick):

		value = self._read() # read the sensor value
		self.values.appendleft([ tick, value ]) # push onto buffer
		self.values.pop() # remove oldest
		return value

	def __repr__(self):
		s = "Sensor {0} type: {1}, unit: {2}, range: {3}".format(self.id, self.type, self.unit, self.range)
		return s


class _Channel:
	def __init__(self, id, bus_type, bus_index, bus_device_index, rra, error, sensors):
		self.id = id
		self.bus_type = bus_type
		self.bus_index = bus_index
		self.bus_device_index = bus_device_index
		self.rra = rra
		self.error = error
		self.sensors = sensors

	def __repr__(self):
		s = "Channel {0} has {1} sensors".format(self.id, len(self.sensors))
		for k, v in self.sensors.items():
			s += "\n\t{0}".format(repr(v))
		return s



class _VirtualChannel:
	def __init__(self, id, rra, error, sensors):
		self.id = id
		self.rra = rra
		self.error = error
		self.sensors = sensors

	def __repr__(self):
		s = "VirtualChannel {0} has {1} sensors".format(self.id, len(self.sensors))
		for k, v in self.sensors.items():
			s += "\n\t{0}".format(repr(v))
		return s



class Avalanche(object):

	_Channels = {} # dict of _Channel

	def __init__(self):

		from .Logging import Logger

		self._logger = Logger # get main logger
		self._logger.info("Setting up GPIO")

		GPIO.setwarnings(False)
		GPIO.setmode(GPIO.BCM)

		#initialize sensor sync pins
		GPIO.setup(AVALANCHE_GPIO_SYNC_SENSOR0, GPIO.OUT, initial=GPIO.HIGH) # TODO: read from config file

		#initialize chip enables
		GPIO.setup(AVALANCHE_GPIO_SPI_CE0, GPIO.OUT, initial=GPIO.LOW)
		GPIO.setup(AVALANCHE_GPIO_SPI_CE1, GPIO.OUT, initial=GPIO.LOW)
		
		#initialize enables/mux control (Optimus)
		GPIO.setup(AVALANCHE_GPIO_STPM34_ENABLE, GPIO.OUT, initial=GPIO.LOW)

		GPIO.setup(AVALANCHE_GPIO_MUX_S0, GPIO.OUT, initial=GPIO.LOW)
		GPIO.setup(AVALANCHE_GPIO_MUX_S1, GPIO.OUT, initial=GPIO.LOW)

		GPIO.setup(AVALANCHE_GPIO_U3_MISO_EN, GPIO.OUT, initial=GPIO.LOW)
		GPIO.setup(AVALANCHE_GPIO_U7_MISO_EN, GPIO.OUT, initial=GPIO.LOW)

		GPIO.setup(AVALANCHE_GPIO_MUX_PUPD_CNTL, GPIO.OUT, initial=GPIO.LOW)

		self.tick = 0 # tracks sync time

		self._logger.info("Enable/Powerup SPI devices")
		self.enableSequence()

		self._logger.info("Setup channels")
		self.setupChannels()


	def sensorPower(self, state):
		'''
		The sensor power bus is controlled via p-channel MOSFET. If output is High,
		bus power is off. If output is Low, power is sent to sensor bus.
		'''
		if state == True:
			outputState = GPIO.LOW
		else:
			outputState = GPIO.HIGH

		GPIO.output(AVALANCHE_GPIO_SENSOR_POWER, outputState)


	def spiBus0isolate(self, state):
		'''
		Bus isolator will isolate the bus if the output is high. The bus is
		enabled if the output is low.
		'''
		if state == True:
			outputState = GPIO.HIGH
		else:
			outputState = GPIO.LOW

		GPIO.output(AVALANCHE_GPIO_ISOLATE_SPI_BUS, outputState)


	def setupSpiChannel(self, ch_id, spi_config, sensors):

		# setup a new SPI channel using a STPM3X device
		bus_index = spi_config['bus_index']
		device_type = spi_config['device_type']
		device_index = spi_config['device_index']

		ch_rra = spi_config['rra']
		
		# configure SPI bus
		spi = spidev.SpiDev()
		spi.open(bus_index, 0)
		spi.mode = 3 # (CPOL = 1 | CPHA = 1) (0b11)

		# configure based on device type
		if device_type == 'STPM3X':

			# Create an STPM3X device object on the SPI bus and pass the configuration.
			# See the STPM3X and Config class in the same module for configuration keys that
			# can be set here to override the defaults in the module.
			self.selectSensor(device_index)
			stpm3x = Stpm3x(spi, spi_config)

			# Construct a list of sensors for which we have configuration objects (passed in on sensors)
			_sensors = {}

			for i, s in enumerate(sensors):

				s_id = s['id']
				s_config = s['_config']

				# There are some constraints on the sensor type and units as these strings
				# are used to construct the RRD data stream attributes.  For now, we have
				# type = [ "VAC", "CAC"] for AC RMS voltage, and AC RMS current types with
				# corresponding units = [ "Vrms", "Arms"] for RMS volts and amps.
				s_type = s_config['type']
				s_units = s_config['units']
				s_range = s_config.get('range', [])

				# Note that the sensor SCALE factor is set during device
				# calibration.  Here are a few scale factors we used for
				# various configurations:
				#
				#	0.035484044 : voltage scale for EVALSTPM34 board (AC Edge demo)
				#	0.056499432 : voltage scale for Optimus first proof-of-concept demo
				#	0.003429594 : current scale for AC Edge and Optimus POC
				s_register = s_config['register']
				s_scale = s_config['scale']
				s_threshold = s_config.get('threshold', None)

				# The STPM3X sensor read function as a closure to
				# capture register, scale, and threshold config
				def s_read(device, register, scale, threshold):

					def r():
						self.selectSensor(device)
						v = stpm3x.read(register, threshold) * scale
						#print("\t{0} (scale: {1}, threshold: {2}) = {3}".format(register, scale, threshold, v))
						return v

					return r

				# Add the sensor the the _sensors for the Channel
				_sensors[s_id] = _Sensor(s_id, s_type, s_units, s_range, s_read(device_index, s_register, s_scale, s_threshold))
				self._logger.info("\tSTPMX3 device sensor added (register: {0}, type: {1}, units: {2})".format(s_register, s_type, s_units))	

			self._Channels[ch_id] = _Channel(ch_id, "SPI", bus_index, device_index, ch_rra, stpm3x.error, _sensors)
			self._logger.info("CHANNEL ADDED: {0} SPI[{1}, {2}] STPM3X device with {3} sensors.\n\n".format(ch_id, bus_index, device_index, len(_sensors)))

		else:
			self._logger.error("SPI channel setup unknown device type {0}".format(device_type))
			return

				
	def setupVirtualChannel(self, ch_id, virtual_config, sensors):
		'''
		Virtual channel can pull data from other channels and combine according to 
		methods provided in the configuration.
		'''
		self._logger.info("Configuring VIRTUAL channel {0}".format(ch_id))

		ch_rra = virtual_config['rra']

		_sensors = {} # added to Channels as a dict
		for i, s in enumerate(sensors):

			s_id = s['id'] # required id

			s_config = s['_config']
			s_type = s_config['type']
			s_units = s_config['units'] # % - but will not be used in DS name
			s_range = s_config['range']

			# Virtual channel sensors can combine the values from other
			# channels (configured from sources) depending on the type
			# set for the sensor.
			s_sources = s_config['sources'] # [ chId.sId, ...]

			# The STPM3X sensor read function as a closure to
			# capture register, scale, and threshold config
			def s_read(sources, stype):
				def r():

					# find the source sensors
					_sources = []
					for src in sources:
						ref = src.split('.') # [ chId, sId ]
						ch = self._Channels.get(ref[0], None)
						if ch:
							s = ch.get(ref[1], None)
						if s: 
							_sources.append(s) # ref to sensor source for virtual channel

					if stype == 'PIB':
						# Phase Imbalance
						if not _sources:
							return

						# Many references for this calculation, but here I'm going
						# to use the maximum difference from average Vrms to calculate
						# 
						Vsum = 0
						for s in _sources:
							Vsum = Vsum + s.values[0]
						Vavg = Vsum / len(_sources)  # RMS average of the phases

						Vmax = 0
						for s in _sources:
							m = abs(Vsum - s.values[0])
							Vmax = m if m > Vmax else Vmax

						return 100 * (Vmax / Vavg) # Phase Imbalance as percentage

					else:
						# Ch error - unknown sensor type
						return

				return r

			# Add the sensor the the _sensors for the Channel
			_sensors[s_id] = _Sensor(s_id, s_type, s_units, s_range, s_read(s_sources, s_type))
			self._logger.info("\tVIRTUAL sensor added (type: {0}, units: {1})".format(s_type, s_units))	

		self._Channels[ch_id] = _VirtualChannel(ch_id, ch_rra, False, _sensors)
		self._logger.info("CHANNEL ADDED: {0} VIRTUAL with {1} sensors.\n\n".format(ch_id, len(_sensors)))


	def setupChannels(self):
		'''
		Attempt to setup channels for each hardware configuration channel found in CHDIR
		'''
		ch_config_pattern = os.path.join(CHDIR, 'ch*_config.json')
		channel_configs = glob.glob(ch_config_pattern) # e.g., [ ".../ch0_config.json", ".../ch1_config.json", ... ]

		count = 0
		for ch_config in sorted(channel_configs):
			# read channel _config into object
			with open(ch_config, 'r') as f:
				try:
					ch = json.load(f)
				except Exception as e:
					self._logger.error("Error loading {0} configuration file: {1}".format(ch_config, e))
					continue

			# configure channel based on bus type
			id = os.path.basename(ch_config).split('_')[0] # take id from filename
			config = ch['_config']
			sensors = ch['sensors']
			bus_type = config['bus_type']

			self._logger.info("\n\nSetting up {0} channel as {1} from {2}".format(bus_type, id, ch_config))

			if bus_type == 'SPI':
				self.setupSpiChannel(id, config, sensors)
				count = count + 1

			elif bus_type == 'VIRTUAL':
				self.setupVirtualChannel(id, config, sensors)
				count = count + 1

			else:
				self._logger.error("Unknown channel bus type {0}".format(bus_type))

		self._logger.info("Done setting up {0} channels".format(count))

		for ch in sorted(self._Channels):
			print("\n{0}: {1}".format(ch, self._Channels[ch]))


	def updateChannels(self):
		'''
		Runs through each channel's sensors and reads their value into the value property
		'''
		self.tick = self.syncSensors()
		
		for ch in self._Channels.values():
			# update sensor values
			if not ch.error:
				for s in ch.sensors.values():
					s.read(self.tick)

		return self._Channels


	def syncSensors(self):
		'''
		STPM3X sensors can be sync'd by briefly pulling the sync line Low for
		each sensor board.
		'''
		GPIO.output(AVALANCHE_GPIO_SYNC_SENSOR0, GPIO.LOW)
		time.sleep(0.001)
		GPIO.output(AVALANCHE_GPIO_SYNC_SENSOR0, GPIO.HIGH)
		time.sleep(0.001)

		return time.time()


	def setSync(self):
		GPIO.output(AVALANCHE_GPIO_SYNC_SENSOR0, GPIO.HIGH)


	def clrSync(self):
		GPIO.output(AVALANCHE_GPIO_SYNC_SENSOR0, GPIO.LOW)


	def chipEnableControl(self, channel, state):
		'''
		Relays are SPST. If the output is high, the relay will close its normally
		open contact
		'''
		if state == True:
			CE_State = GPIO.HIGH
		else:
			CE_State = GPIO.LOW

		if channel == 1:
			GPIO.output(AVALANCHE_GPIO_SPI_CE0, CE_State)
		elif channel == 2:
			GPIO.output(AVALANCHE_GPIO_SPI_CE1, CE_State)

	def enableSensors(self):
		GPIO.output(AVALANCHE_GPIO_STPM34_ENABLE, GPIO.HIGH)

	def disableSensors(self):
		GPIO.output(AVALANCHE_GPIO_STPM34_ENABLE, GPIO.LOW)

	def setMuxOuputsPullup(self):
		GPIO.output(AVALANCHE_GPIO_MUX_PUPD_CNTL, GPIO.HIGH)

	def setMuxOuputsPulldown(self):
		GPIO.output(AVALANCHE_GPIO_MUX_PUPD_CNTL, GPIO.LOW)


	def selectSensor(self, device):
		if device == 1: #SS1
			#print("Select Sensor SS1 - BANK 1")
			self.selectVoltageBank(1)
			GPIO.output(AVALANCHE_GPIO_MUX_S0, GPIO.HIGH)
			GPIO.output(AVALANCHE_GPIO_MUX_S1, GPIO.LOW)
		elif device == 2: #SS2
			#print("Select Sensor SS2 - BANK 1")
			self.selectVoltageBank(1)
			GPIO.output(AVALANCHE_GPIO_MUX_S0, GPIO.LOW)
			GPIO.output(AVALANCHE_GPIO_MUX_S1, GPIO.LOW)		
		elif device == 3: #SS3 or SS4
			#print("Select Sensor SS3 - BANK 2")
			self.selectVoltageBank(2)
			GPIO.output(AVALANCHE_GPIO_MUX_S0, GPIO.HIGH)
			GPIO.output(AVALANCHE_GPIO_MUX_S1, GPIO.HIGH)
		elif device == 4: #SS3 or SS4
			#print("Select Sensor SS4 - BANK 2")
			self.selectVoltageBank(2)
			GPIO.output(AVALANCHE_GPIO_MUX_S0, GPIO.LOW)
			GPIO.output(AVALANCHE_GPIO_MUX_S1, GPIO.HIGH)


	def selectVoltageBank(self, bank):
		if bank == 1:
			GPIO.output(AVALANCHE_GPIO_U3_MISO_EN, GPIO.HIGH)
			GPIO.output(AVALANCHE_GPIO_U7_MISO_EN, GPIO.LOW)
		elif bank == 2:
			GPIO.output(AVALANCHE_GPIO_U3_MISO_EN, GPIO.LOW)
			GPIO.output(AVALANCHE_GPIO_U7_MISO_EN, GPIO.HIGH)
		else:
			GPIO.output(AVALANCHE_GPIO_U3_MISO_EN, GPIO.LOW)
			GPIO.output(AVALANCHE_GPIO_U7_MISO_EN, GPIO.LOW)

	def enableSequence(self):
		#STPM init sequence  

		#Ensure all SSx lines are low (PUPD_CTRL = LOW) before enabling the chips with the enable signal
		self.disableSensors()
		self.setMuxOuputsPulldown()
		self.chipEnableControl(1, False)
		time.sleep(0.5);

		#Enable the sensors chips by transitioning 
		self.enableSensors()
		time.sleep(0.2);

		#Set Lines to their default state, SSx lines are HIGH, Sync is HIGH, CE0 is high
		self.setSync()
		self.setMuxOuputsPullup()
		self.chipEnableControl(1, True)
		time.sleep(0.1)

		#Send the Global Software Reset signal (3 1ms LOW pulses followed by a 1ms LOW Pulse on SSx lines)
		self.syncSensors()
		self.syncSensors()
		self.syncSensors()

		self.chipEnableControl(1, False)
		self.setMuxOuputsPulldown()
		time.sleep(0.001);
		self.chipEnableControl(1, True)
		self.setMuxOuputsPullup()