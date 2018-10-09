# pymill

Python3 library for Mill.
Based on https://pastebin.com/53Nk0wJA and Postman capturing from the app.
All requests are send unencrypted from the app :(

Control Mill heaters and get meassured temperature. 

## Install
```
pip3 install pymill
```

## Example:

```python
import mill
mill_connection = mill.Mill('email@gmail.com', 'PASSWORD')
mill_connection.sync_connect()
mill_connection.sync_update_heaters()

heater = next(iter(mill_connection.heaters.values()))

mill_connection.sync_set_heater_temp(heater.device_id, 11)
mill_connection.sync_set_heater_control(heater.device_id, fan_status=0)

mill_connection.sync_close_connection()

```

The library is used as part of Home Assitant: [https://github.com/home-assistant/home-assistant/blob/dev/homeassistant/components/climate/mill.py](https://github.com/home-assistant/home-assistant/blob/dev/homeassistant/components/climate/mill.py)
