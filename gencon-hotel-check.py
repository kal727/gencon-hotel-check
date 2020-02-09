#!/usr/bin/env python2
from __future__ import print_function
from argparse import Action, ArgumentParser, ArgumentError, ArgumentTypeError, SUPPRESS
from datetime import datetime, timedelta
from json import loads as fromJS, dumps as toJS
from os.path import abspath, dirname, join as pathjoin
from re import compile as reCompile, IGNORECASE as RE_IGNORECASE
from ssl import create_default_context as create_ssl_context, CERT_NONE, SSLError
from sys import stdout, version_info
from threading import Thread
from time import sleep
from pushbullet import Pushbullet
import requests

if version_info < (2, 7, 9):
    	print("Requires Python 2.7.9+")
	exit(1)
elif version_info.major == 2:
	from HTMLParser import HTMLParser
	from urllib import urlencode
	from urllib2 import HTTPCookieProcessor, HTTPError, Request, URLError, urlopen, build_opener
else:
	from html.parser import HTMLParser
	from urllib.error import HTTPError, URLError
	from urllib.parse import urlencode
	from urllib.request import HTTPCookieProcessor, Request, urlopen, build_opener

firstDay, lastDay, startDay = datetime(2020, 7, 29), datetime(2020, 8, 2), datetime(2020, 7, 30)


eventId = 50023680
ownerId = 10909638

targetUrl = ''

distanceUnits = {
	1: 'blocks',
	2: 'yards',
	3: 'miles',
	4: 'meters',
	5: 'kilometers',
}

class PasskeyParser(HTMLParser):
	def __init__(self, resp):
		HTMLParser.__init__(self)
		self.json = None
		self.feed(resp.read().decode('utf8'))
		self.close()

	def handle_starttag(self, tag, attrs):
		if tag.lower() == 'script':
			attrs = dict(attrs)
			if attrs.get('id', '').lower() == 'last-search-results':
				self.json = True

	def handle_data(self, data):
		if self.json is True:
			self.json = data

try:
	from html import unescape
	PasskeyParser.unescape = lambda self, text: unescape(text)
except ImportError as e:
	pass

def type_day(arg):
	try:
		d = datetime.strptime(arg, '%Y-%m-%d')
	except ValueError:
		raise ArgumentTypeError("%s is not a date in the form YYYY-MM-DD" % arg)
	if not firstDay <= d <= lastDay:
		raise ArgumentTypeError("%s is outside the Gencon housing block window" % arg)
	return arg

def type_distance(arg):
	if arg == 'connected':
		return arg
	try:
		return float(arg)
	except ValueError:
		raise ArgumentTypeError("invalid float value: '%s'" % arg)

def type_regex(arg):
	try:
		return reCompile(arg, RE_IGNORECASE)
	except Exception as e:
		raise ArgumentTypeError("invalid regex '%s': %s" % (arg, e))

class PasskeyUrlAction(Action):
    def __call__(self, parser, namespace, values, option_string = None):
		m = reCompile('^https://book.passkey.com/reg/([0-9A-Z]{8}-[0-9A-Z]{4})/([0-9a-f]{1,64})$').match(values)
		if m:
			setattr(namespace, self.dest, m.groups())
		else:
			raise ArgumentError(self, "invalid passkey url: '%s'" % values)

parser = ArgumentParser()
parser.add_argument('--guests', type = int, default = 1, help = 'number of guests')
parser.add_argument('--children', type = int, default = 0, help = 'number of children')
parser.add_argument('--rooms', type = int, default = 1, help = 'number of rooms')
group = parser.add_mutually_exclusive_group()
group.add_argument('--checkin', type = type_day, metavar = 'YYYY-MM-DD', default = (startDay - timedelta(1)).strftime('%Y-%m-%d'), help = 'check in')
group.add_argument('--wednesday', dest = 'checkin', action = 'store_const', const = (startDay - timedelta(1)).strftime('%Y-%m-%d'), help = 'check in on Wednesday')
parser.add_argument('--checkout', type = type_day, metavar = 'YYYY-MM-DD', default = (startDay + timedelta(3)).strftime('%Y-%m-%d'), help = 'check out')
group = parser.add_mutually_exclusive_group()
group.add_argument('--max-distance', type = type_distance, metavar = 'BLOCKS', help = "max hotel distance that triggers an alert (or 'connected' to require skywalk hotels)")
group.add_argument('--connected', dest = 'max_distance', action = 'store_const', const = 'connected', help = 'shorthand for --max-distance connected')
parser.add_argument('--budget', type = float, metavar = 'PRICE', default = '99999', help = 'max total rate (not counting taxes/fees) that triggers an alert')
parser.add_argument('--hotel-regex', type = type_regex, metavar = 'PATTERN', default = reCompile('.*'), help = 'regular expression to match hotel name against')
parser.add_argument('--room-regex', type = type_regex, metavar = 'PATTERN', default = reCompile('.*'), help = 'regular expression to match room against')
parser.add_argument('--show-all', action = 'store_true', help = 'show all rooms, even if miles away (these rooms never trigger alerts)')
group = parser.add_mutually_exclusive_group()
group.add_argument('--delay', type = int, default = 1, metavar = 'MINS', help = 'search every MINS minute(s)')
group.add_argument('--once', action = 'store_true', help = 'search once and exit')
parser.add_argument('--test', action = 'store_true', dest = 'test', help = 'trigger every specified alert and exit')

group = parser.add_argument_group('required arguments')
# Both of these set 'key'; only one of them is required
group.add_argument('--key', nargs = 2, metavar = ('KEY', 'AUTH'), help = 'key (see the README for more information)')
group.add_argument('--url', action = PasskeyUrlAction, dest = 'key', help = 'passkey URL containing your key')

args = parser.parse_args()

baseUrl = "https://book.passkey.com/event/%d/owner/%d" % (eventId, ownerId)

def notifyPushbullet():
    	pushbulletKey = ''
    	pb = Pushbullet(pushbulletKey)
	pb.push_link("Gencon Housing Notification", targetUrl)

def notifyDiscord():
    discordWebhookUrl = ''
	message = "Gencon Downton Hotel Notification: "+ targetUrl
	data = {"content":message}
	requests.post(discordWebhookUrl, data)

if args.test:
	print("Testing alerts one at a time...")

	print("Testing Pushbullet...")
	notifyPushbullet()

	print("Testing Discord...")
	notifyDiscord()
	
	print("Done")
 	exit(0)

opener = build_opener(HTTPCookieProcessor())

def send(name, *args):
   	try:
		resp = opener.open(*args)
		if resp.getcode() != 200:
			raise RuntimeError("%s failed: %d" % (name, resp.getcode()))
		return resp
	except URLError as e:
		raise RuntimeError("%s failed: %s" % (name, e))

def searchNew():
	'''Search using a reservation key (for users who don't have a booking yet)'''
	resp = send('Session request', "https://book.passkey.com/reg/%s/%s" % tuple(args.key))
	data = {
		'blockMap.blocks[0].blockId': '0',
		'blockMap.blocks[0].checkIn': args.checkin,
		'blockMap.blocks[0].checkOut': args.checkout,
		'blockMap.blocks[0].numberOfGuests': str(args.guests),
		'blockMap.blocks[0].numberOfRooms': str(args.rooms),
		'blockMap.blocks[0].numberOfChildren': str(args.children),
	}
	return send('Search', baseUrl + '/rooms/select', urlencode(data).encode('utf8'))

def searchExisting(hash = []):
	'''Search using an acknowledgement number (for users who have booked a room)'''
	# The hash doesn't change, so it's only calculated the first time
	if not hash:
		send('Session request', baseUrl + '/home')
		data = {
			'ackNum': args.key[0],
			'lastName': args.key[1],
		}
		resp = send('Finding reservation', Request(baseUrl + '/reservation/find', toJS(data).encode('utf8'), {'Content-Type': 'application/json'}))
		try:
			respData = fromJS(resp.read())
		except Exception as e:
			raise RuntimeError("Failed to decode reservation: %s" % e)
		if respData.get('ackNum', None) != args.key[0]:
			raise RuntimeError("Reservation not found. Are your acknowledgement number and surname correct?")
		if 'hash' not in respData:
			raise RuntimeError("Hash missing from reservation data")
		hash.append(respData['hash'])

	data = {
		'blockMap': {
			'blocks': [{
				'blockId': '0',
				'checkIn': args.checkin,
				'checkOut': args.checkout,
				'numberOfGuests': str(args.guests),
				'numberOfRooms': str(args.rooms),
				'numberOfChildren': str(args.children),
			}]
		},
	}
	send('Loading existing reservation', baseUrl + "/r/%s/%s" % (args.key[0], hash[0]))
	send('Search', Request(baseUrl + '/rooms/select/search', toJS(data).encode('utf8'), headers = {'Content-Type': 'application/json'}))

def parseResults():
	resp = send('List', baseUrl + '/list/hotels')
	parser = PasskeyParser(resp)
	if not parser.json:
		raise RuntimeError("Failed to find search results")

	hotels = fromJS(parser.json)

	print("Results:   (%s)" % datetime.now())
	alerts = []

	print("   %-15s %-10s %-80s %s" % ('Distance', 'Price', 'Hotel', 'Room'))
	for hotel in hotels:
		for block in hotel['blocks']:
			# Don't show hotels miles away unless requested
			if hotel['distanceUnit'] == 3 and not args.show_all:
				continue

			connected = ('Skywalk to ICC' in (hotel['messageMap'] or ''))
			simpleHotel = {
				'name': parser.unescape(hotel['name']),
				'distance': 'Skywalk' if connected else "%4.1f %s" % (hotel['distanceFromEvent'], distanceUnits.get(hotel['distanceUnit'], '???')),
				'price': int(sum(inv['rate'] for inv in block['inventory'])),
				'rooms': min(inv['available'] for inv in block['inventory']),
				'room': parser.unescape(block['name']),
			}
			if simpleHotel['rooms'] == 0:
				continue
			result = "%-15s $%-9s %-80s (%d) %s" % (simpleHotel['distance'], simpleHotel['price'], simpleHotel['name'], simpleHotel['rooms'], simpleHotel['room'])

			#nameMatch = ('Fair' in simpleHotel['name']) or ('fair' in simpleHotel['name']) or ('Spring' in simpleHotel['name']) or ('spring' in simpleHotel['name']) or ('Court' in simpleHotel['name']) or ('court' in simpleHotel['name']) or ('Embassy' in simpleHotel['name']) or ('embassy' in simpleHotel['name'])

			#connected = True

			# I don't think these distances (yards, meters, kilometers) actually appear in the results, but if they do assume it must be close enough regardless of --max-distance
			closeEnough = connected \
			#Uncomment for blocks
			#or hotel['distanceUnit'] == 1 or hotel['distanceFromEvent'] == 1 or hotel['distanceUnit'] in (2, 4, 5) or \
			#(hotel['distanceUnit'] == 1 and (args.max_distance is None or (isinstance(args.max_distance, float) and hotel['distanceFromEvent'] <= args.max_distance))) or \
			(args.max_distance == 'connected' and connected)
			#if closeEnough and nameMatch:
			if closeEnough:
				alerts.append(simpleHotel)
				stdout.write(' ! ')
			else:
				stdout.write('   ')
			print(result)

	if alerts:
    		notifyPushbullet()
		notifyDiscord()
		return True

search = searchNew if '-' in args.key[0] else searchExisting
while True:
	print("Searching... (%d %s, %d %s, %s - %s, %s)" % (args.guests, 'guest' if args.guests == 1 else 'guests', args.rooms, 'room' if args.rooms == 1 else 'rooms', args.checkin, args.checkout, 'connected' if args.max_distance == 'connected' else 'downtown' if args.max_distance is None else "within %.1f blocks" % args.max_distance))
	try:
		search()
		parseResults()
	except Exception as e:
		print(str(e))
	if args.once:
		exit(0)
	sleep(30)